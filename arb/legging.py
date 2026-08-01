"""Sequential legging: entry, re-quote, unwind.

The exposure window between leg 1 and leg 2 is the only moment this system
holds an unhedged binary, and every path through this module either closes it
into a Position or unwinds it. Nothing here waits, retries indefinitely, or
leaves a pair half-open.

The recovery ladder, in order:

1. Leg 2 at the planned limit, capped by the breakeven implied by leg 1's
   actual fill.
2. On rejection, one re-quote up to that breakeven - a moved market is
   salvaged rather than abandoned.
3. Beyond breakeven, unwind leg 1 at market. The worst case is then a spread
   crossing plus fees, which is a cost; holding a naked binary is not.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Iterable

from arb.actions import Action, Alert, CancelOrder
from arb.domain import BookSnapshot, Level, MatchedPair, Venue
from arb.execution import breakeven_price, leg_difficulty
from arb.orders import mint_order
from arb.pricing import fee_breakdown
from arb.sizing import Sizing
from arb.state import PendingEntry, Position, State, UnwindIncident

__all__ = [
    "abandon_entry",
    "begin_entry",
    "on_fill",
    "on_reject",
    "sweep_stuck_entries",
]

ZERO = Decimal("0")


def begin_entry(
    state: State, pair: MatchedPair, sized: Sizing, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """Place leg 1 - the harder side - and open the pending entry."""
    kalshi = state.books[pair.key_on("kalshi")]
    polymarket = state.books[pair.key_on("polymarket")]
    kalshi_limit = sized.kalshi_limit_price or ZERO
    polymarket_limit = sized.polymarket_limit_price or ZERO

    first_venue = _harder_leg(
        state,
        sized.size,
        kalshi=(kalshi, kalshi_limit),
        polymarket=(polymarket, polymarket_limit),
    )

    pending = PendingEntry(
        pair_id=pair.pair_id,
        intended_size=sized.size,
        first_venue=first_venue,
        category=pair.category,
        settlement_source=pair.settlement_source,
        settlement_date=pair.settlement_date,
        predicted_net_edge=sized.expected_profit / sized.size,
        kalshi_limit=kalshi_limit,
        polymarket_limit=polymarket_limit,
        opened_at_ms=at_ms,
    )

    state, order = mint_order(
        state,
        pair=pair,
        venue=first_venue,
        side="buy",
        size=sized.size,
        limit_price=pending.price_on(first_venue),
        purpose="leg1",
    )
    # Republished immediately: the pair's notional is committed the moment
    # leg 1 is placed, so it must consume the concentration budgets before the
    # next candidate is evaluated against them.
    state = replace(state, pending={**state.pending, pair.pair_id: pending})
    return state.with_republished_risk(), (order,)


def on_fill(
    state: State, order_id: str, size: int, price: Decimal, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """Route a fill - full or partial - to whichever leg it belongs to."""
    ref = state.orders.get(order_id)
    if ref is None:
        return state, ()
    pending = state.pending.get(ref.pair_id)
    if pending is None:
        return state, ()
    state = _forget_order(state, order_id)

    match ref.purpose:
        case "leg1":
            return _after_leg_one(state, pending, size, price)
        case "leg2":
            return _after_leg_two(state, pending, size, price, at_ms)
        case "unwind":
            return _after_unwind(state, pending, size, price, at_ms)
        case _:
            return state, ()


def on_reject(state: State, order_id: str) -> tuple[State, tuple[Action, ...]]:
    ref = state.orders.get(order_id)
    if ref is None:
        return state, ()
    pending = state.pending.get(ref.pair_id)
    if pending is None:
        return state, ()
    state = _forget_order(state, order_id)

    if ref.purpose == "leg1":
        # Nothing filled, so nothing is exposed. Not a Leg Failure - counting
        # it would exhaust the budget on non-events.
        return _clear(state, pending.pair_id).with_republished_risk(), ()

    if ref.purpose == "leg2":
        return _requote_or_unwind(state, pending)

    # An unwind that itself gets rejected is beyond what this ladder can fix.
    return _clear(state, pending.pair_id), (
        Alert(
            severity="critical",
            message="unwind rejected; position may be unhedged",
            pair_id=pending.pair_id,
        ),
    )


def sweep_stuck_entries(
    state: State, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """Unwind entries the venue never answered.

    Driven by `Timer` because time is the only signal that can break this
    deadlock, and time reaches the reducer only as an event. A timeout *is* an
    execution failure - the venue did not answer - so unlike an abandoned entry
    it counts against the Leg Failure budget.
    """
    timeout = state.config.entry_timeout_ms
    if timeout <= 0:
        return state, ()

    stale = [
        pending
        for pair_id, pending in sorted(state.pending.items())
        if at_ms - pending.opened_at_ms > timeout
    ]

    actions: list[Action] = []
    for pending in stale:
        state, timed_out = _time_out_entry(state, pending)
        actions.extend(timed_out)
    return state, tuple(actions)


def _time_out_entry(
    state: State, pending: PendingEntry
) -> tuple[State, tuple[Action, ...]]:
    cancels, state = _cancel_working_orders(state, pending.pair_id)
    if pending.first_filled_size > 0:
        state, unwind_actions = _unwind(
            state, pending, pending.first_filled_size, "venue did not answer in time"
        )
        return state, cancels + unwind_actions
    # Nothing filled, so nothing is exposed and nothing needs unwinding.
    return _clear(state, pending.pair_id).with_republished_risk(), cancels


def _cancel_working_orders(
    state: State, pair_id: str
) -> tuple[tuple[Action, ...], State]:
    """Pull any live entry order for this pair, so the venue cannot fill it
    into a pair the system has stopped waiting for."""
    cancels = tuple(
        CancelOrder(order_id=order_id, venue=ref.venue)
        for order_id, ref in sorted(state.orders.items())
        if ref.pair_id == pair_id and ref.purpose in ("leg1", "leg2")
    )
    cancelled = {cancel.order_id for cancel in cancels}
    state = replace(
        state,
        orders={k: v for k, v in state.orders.items() if k not in cancelled},
    )
    return cancels, state


def abandon_entry(
    state: State, pair_id: str, reason: str
) -> tuple[State, tuple[Action, ...]]:
    """Stop an entry mid-flight and undo whatever it has already done.

    Called when something outside execution - a dispute, a postponement, a rule
    divergence - means the pair is no longer worth completing. Any live order is
    cancelled so the venue cannot fill it into a pair the system has just
    abandoned, and any leg that already filled is unwound.

    This is not a Leg Failure: execution did not fail, the market did, and
    charging it to the Leg Failure budget would halt the system for someone
    else's problem.
    """
    pending = state.pending.get(pair_id)
    if pending is None:
        return state, ()

    cancels, state = _cancel_working_orders(state, pair_id)

    if pending.first_filled_size > 0:
        state, unwind_actions = _unwind(
            state, pending, pending.first_filled_size, reason, counts_as_failure=False
        )
        return state, cancels + unwind_actions

    return _clear(state, pair_id).with_republished_risk(), cancels


def _after_leg_one(
    state: State, pending: PendingEntry, size: int, price: Decimal
) -> tuple[State, tuple[Action, ...]]:
    """Leg 1 filled. Size leg 2 to what actually filled, bounded by breakeven."""
    if size <= 0:
        return _clear(state, pending.pair_id).with_republished_risk(), ()

    pending = replace(pending, first_filled_size=size, first_fill_price=price)
    state = replace(state, pending={**state.pending, pending.pair_id: pending})

    limit = _leg_two_limit(state, pending)
    if limit <= 0:
        return _unwind(state, pending, size, "no price leaves the pair profitable")

    pair = state.pair_registry[pending.pair_id]
    state, order = mint_order(
        state,
        pair=pair,
        venue=pending.second_venue,
        side="buy",
        size=size,
        limit_price=limit,
        purpose="leg2",
    )
    return state, (order,)


def _after_leg_two(
    state: State, pending: PendingEntry, size: int, price: Decimal, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """Leg 2 filled. Whatever matched becomes a Position; any surplus on leg 1
    is unwound, because an unmatched leg is the exposure this system exists to
    avoid."""
    matched = min(pending.first_filled_size, size)
    remainder = pending.first_filled_size - matched

    if remainder > 0:
        # A partial leg 2 is a Leg Failure by the spec's definition, and
        # recovery applies to the unmatched remainder only. The pending entry
        # survives until the unwind reports, so its notional stays reserved -
        # over-reserving while a recovery is in flight is the safe direction.
        state = _open_position(state, pending, matched, price, at_ms)
        return _unwind(state, pending, remainder, "leg 2 filled partially")

    # Pending is cleared *before* the position is published, so the two never
    # both count. They are the same capital, and publishing it twice makes the
    # next candidate in this same book batch read a doubled exposure.
    state = _clear(state, pending.pair_id)
    return _open_position(state, pending, matched, price, at_ms), ()


def _after_unwind(
    state: State, pending: PendingEntry, size: int, price: Decimal, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """Record what the Leg Failure actually cost, rather than assuming it."""
    incident = UnwindIncident(
        pair_id=pending.pair_id,
        size=size,
        entry_price=pending.first_fill_price,
        exit_price=price,
        at_ms=at_ms,
    )
    state = replace(state, unwind_incidents=state.unwind_incidents + (incident,))
    return _clear(state, pending.pair_id).with_republished_risk(), ()


def _requote_or_unwind(
    state: State, pending: PendingEntry
) -> tuple[State, tuple[Action, ...]]:
    """One re-quote up to breakeven, then unwind."""
    breakeven = _breakeven_for(state, pending)
    planned = pending.price_on(pending.second_venue)

    if not pending.requoted and breakeven > planned:
        pending = replace(pending, requoted=True)
        state = replace(state, pending={**state.pending, pending.pair_id: pending})
        pair = state.pair_registry[pending.pair_id]
        state, order = mint_order(
            state,
            pair=pair,
            venue=pending.second_venue,
            side="buy",
            size=pending.first_filled_size,
            limit_price=breakeven,
            purpose="leg2",
        )
        return state, (order,)

    return _unwind(
        state, pending, pending.first_filled_size, "leg 2 could not fill inside breakeven"
    )


def _unwind(
    state: State,
    pending: PendingEntry,
    size: int,
    reason: str,
    *,
    counts_as_failure: bool = True,
) -> tuple[State, tuple[Action, ...]]:
    """Sell leg 1 back at market.

    `counts_as_failure` is false when the entry was abandoned for reasons
    outside execution, so that the Leg Failure budget measures what it is named
    for rather than absorbing every reason a pair was dropped.
    """
    pair = state.pair_registry[pending.pair_id]
    book = state.books.get(pair.key_on(pending.first_venue))
    bid = book.best_bid if book else None

    if counts_as_failure:
        state = replace(state, leg_failures=state.leg_failures + 1)
    alert = Alert(
        severity="critical",
        message=(
            f"leg failure on {pending.pair_id}: {reason}"
            if counts_as_failure
            else f"entry abandoned on {pending.pair_id}: {reason}"
        ),
        pair_id=pending.pair_id,
    )

    if bid is None:
        # Nothing to sell into. Say so loudly rather than silently holding an
        # unhedged binary.
        return _clear(state, pending.pair_id).with_republished_risk(), (
            alert,
            Alert(
                severity="critical",
                message="no bid to unwind into; leg 1 is unhedged",
                pair_id=pending.pair_id,
            ),
        )

    state, order = mint_order(
        state,
        pair=pair,
        venue=pending.first_venue,
        side="sell",
        size=size,
        limit_price=bid.price,
        purpose="unwind",
    )
    return state.with_republished_risk(), (order, alert)


def _open_position(
    state: State, pending: PendingEntry, size: int, second_price: Decimal, at_ms: int
) -> State:
    if size <= 0:
        return state

    first_notional = pending.first_fill_price * size
    second_notional = second_price * size
    kalshi_notional, polymarket_notional = (
        (first_notional, second_notional)
        if pending.first_venue == "kalshi"
        else (second_notional, first_notional)
    )

    # Fees are charged on what actually filled, not on what was quoted, so that
    # realised profit reconciles against a real number rather than an intent.
    pair = state.pair_registry[pending.pair_id]
    schedule = state.config.fees_for(pair.category)
    fees_paid = (
        fee_breakdown(kalshi_notional / size, polymarket_notional / size, schedule).total
        * size
        if schedule is not None
        else ZERO
    )

    position = Position(
        pair_id=pending.pair_id,
        size=size,
        kalshi_notional=kalshi_notional,
        polymarket_notional=polymarket_notional,
        category=pending.category,
        settlement_source=pending.settlement_source,
        settlement_date=pending.settlement_date,
        opened_at_ms=at_ms,
        predicted_net_edge=pending.predicted_net_edge,
        fees_paid=fees_paid,
    )
    return replace(
        state, positions=state.positions + (position,)
    ).with_republished_risk()


def _leg_two_limit(state: State, pending: PendingEntry) -> Decimal:
    """The planned limit, capped by breakeven. Breakeven is a ceiling."""
    return min(pending.price_on(pending.second_venue), _breakeven_for(state, pending))


def _breakeven_for(state: State, pending: PendingEntry) -> Decimal:
    pair = state.pair_registry[pending.pair_id]
    schedule = state.config.fees_for(pair.category)
    if schedule is None:
        return ZERO
    return breakeven_price(
        filled_leg_price=pending.first_fill_price,
        filled_venue=pending.first_venue,
        schedule=schedule,
    )


def _harder_leg(
    state: State,
    size: int,
    *,
    kalshi: tuple[BookSnapshot, Decimal],
    polymarket: tuple[BookSnapshot, Decimal],
) -> Venue:
    """Whichever leg is expected to be harder to fill goes first.

    Ties resolve to Kalshi - arbitrary, but it has to be *some* fixed venue so
    that the action trace replays identically.
    """
    kalshi_score = leg_difficulty(
        depth=_depth_within(kalshi[0].asks, kalshi[1]),
        intended_size=size,
        latency_ms=state.venue_latency_ms.get("kalshi", 0),
    )
    polymarket_score = leg_difficulty(
        depth=_depth_within(polymarket[0].asks, polymarket[1]),
        intended_size=size,
        latency_ms=state.venue_latency_ms.get("polymarket", 0),
    )
    return "kalshi" if kalshi_score >= polymarket_score else "polymarket"


def _depth_within(levels: Iterable[Level], limit: Decimal) -> int:
    """Contracts available at or better than the limit price."""
    total = 0
    for level in levels:
        if level.price > limit:
            break
        total += level.size
    return total




def _clear(state: State, pair_id: str) -> State:
    """Drop a pending entry, freeing the budget its notional was consuming."""
    pending = {key: value for key, value in state.pending.items() if key != pair_id}
    return replace(state, pending=pending)


def _forget_order(state: State, order_id: str) -> State:
    """Retire an order that has reported its outcome.

    Fills, partial fills and rejections are all terminal, so a second event for
    the same order is a duplicate. Forgetting the id makes the duplicate a
    no-op instead of a second leg 2 and a second Position.
    """
    return replace(
        state,
        orders={key: value for key, value in state.orders.items() if key != order_id},
    )
