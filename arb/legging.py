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

from arb.actions import Action, Alert, PlaceOrder
from arb.domain import BookSnapshot, MatchedPair, Venue
from arb.execution import breakeven_price, leg_difficulty
from arb.sizing import Sizing
from arb.state import OrderRef, PendingEntry, Position, State

__all__ = ["begin_entry", "on_fill", "on_reject"]

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
    )

    state, order = _place(
        state,
        pair=pair,
        venue=first_venue,
        side="buy",
        size=sized.size,
        limit_price=pending.price_on(first_venue),
        purpose="leg1",
    )
    return replace(state, pending={**state.pending, pair.pair_id: pending}), (order,)


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

    match ref.purpose:
        case "leg1":
            return _after_leg_one(state, pending, size, price, at_ms)
        case "leg2":
            return _after_leg_two(state, pending, size, price, at_ms)
        case "unwind":
            return _after_unwind(state, pending, size, price)
        case _:
            return state, ()


def on_reject(state: State, order_id: str, at_ms: int) -> tuple[State, tuple[Action, ...]]:
    ref = state.orders.get(order_id)
    if ref is None:
        return state, ()
    pending = state.pending.get(ref.pair_id)
    if pending is None:
        return state, ()

    if ref.purpose == "leg1":
        # Nothing filled, so nothing is exposed. Not a Leg Failure - counting
        # it would exhaust the budget on non-events.
        return _clear(state, pending.pair_id), ()

    if ref.purpose == "leg2":
        return _requote_or_unwind(state, pending, at_ms)

    # An unwind that itself gets rejected is beyond what this ladder can fix.
    return _clear(state, pending.pair_id), (
        Alert(
            severity="critical",
            message="unwind rejected; position may be unhedged",
            pair_id=pending.pair_id,
        ),
    )


def _after_leg_one(
    state: State, pending: PendingEntry, size: int, price: Decimal, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """Leg 1 filled. Size leg 2 to what actually filled, bounded by breakeven."""
    if size <= 0:
        return _clear(state, pending.pair_id), ()

    pending = replace(pending, first_filled_size=size, first_fill_price=price)
    state = replace(state, pending={**state.pending, pending.pair_id: pending})

    limit = _leg_two_limit(state, pending)
    if limit <= 0:
        return _unwind(state, pending, size, "no price leaves the pair profitable")

    pair = state.pair_registry[pending.pair_id]
    state, order = _place(
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

    state = _open_position(state, pending, matched, price, at_ms)

    if remainder > 0:
        # A partial leg 2 is a Leg Failure by the spec's definition, and
        # recovery applies to the unmatched remainder only.
        return _unwind(state, pending, remainder, "leg 2 filled partially")

    return _clear(state, pending.pair_id), ()


def _after_unwind(
    state: State, pending: PendingEntry, size: int, price: Decimal
) -> tuple[State, tuple[Action, ...]]:
    """Record what the Leg Failure actually cost, rather than assuming it."""
    cost = (pending.first_fill_price - price) * size
    state = replace(state, unwind_cost=state.unwind_cost + cost)
    return _clear(state, pending.pair_id), ()


def _requote_or_unwind(
    state: State, pending: PendingEntry, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """One re-quote up to breakeven, then unwind."""
    breakeven = _breakeven_for(state, pending)
    planned = pending.price_on(pending.second_venue)

    if not pending.requoted and breakeven > planned:
        pending = replace(pending, requoted=True)
        state = replace(state, pending={**state.pending, pending.pair_id: pending})
        pair = state.pair_registry[pending.pair_id]
        state, order = _place(
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
    state: State, pending: PendingEntry, size: int, reason: str
) -> tuple[State, tuple[Action, ...]]:
    """Sell leg 1 back at market and count the Leg Failure."""
    pair = state.pair_registry[pending.pair_id]
    book = state.books.get(pair.key_on(pending.first_venue))
    bid = book.best_bid if book else None

    state = replace(state, leg_failures=state.leg_failures + 1)
    alert = Alert(
        severity="critical",
        message=f"leg failure on {pending.pair_id}: {reason}",
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

    state, order = _place(
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


def _depth_within(levels: Iterable[object], limit: Decimal) -> int:
    """Contracts available at or better than the limit price."""
    total = 0
    for level in levels:
        price = getattr(level, "price")
        if price > limit:
            break
        total += getattr(level, "size")
    return total


def _place(
    state: State,
    *,
    pair: MatchedPair,
    venue: Venue,
    side: str,
    size: int,
    limit_price: Decimal,
    purpose: str,
) -> tuple[State, PlaceOrder]:
    """Mint an order id from state, never from a clock or a random source."""
    sequence = state.order_sequence + 1
    order_id = f"{pair.pair_id}:{purpose}:{sequence}"
    order = PlaceOrder(
        order_id=order_id,
        pair_id=pair.pair_id,
        venue=venue,
        contract_id=pair.contract_on(venue),
        side=side,  # type: ignore[arg-type]
        size=size,
        limit_price=limit_price,
        purpose=purpose,  # type: ignore[arg-type]
    )
    state = replace(
        state,
        order_sequence=sequence,
        orders={
            **state.orders,
            order_id: OrderRef(pair_id=pair.pair_id, venue=venue, purpose=purpose),
        },
    )
    return state, order


def _clear(state: State, pair_id: str) -> State:
    pending = {key: value for key, value in state.pending.items() if key != pair_id}
    return replace(state, pending=pending)
