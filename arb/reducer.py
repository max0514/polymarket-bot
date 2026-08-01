"""The reducer - `step(State, Event) -> (State, Action[])`.

The single seam. All strategy, sizing, risk, and execution logic is reached
through here, which is why replay through this boundary is simultaneously the
test suite, the backtest, and the verdict instrument.

Pure: no clock, no I/O, no randomness. Time and connectivity arrive as events.
"""

from __future__ import annotations

from dataclasses import replace

from arb.actions import Action, EmitDecisionRecord
from arb.decisions import DecisionRecord
from arb.evaluate import evaluate_pair
from arb.events import (
    BalanceUpdate,
    BookUpdate,
    DisputeOpened,
    Event,
    Fill,
    KillSwitch,
    OrderAck,
    PartialFill,
    Postponement,
    Reject,
    RuleDivergenceFound,
    Settlement,
    Timer,
    TunnelHealth,
)
from arb.exits import apply_kill_switch, on_exit_fill, on_exit_reject, trigger_exit
from arb.inventory import rank_candidates
from arb.legging import begin_entry, on_fill, on_reject, sweep_stuck_entries
from arb.settlement import on_settlement
from arb.sizing import walk
from arb.state import KillTier, State

__all__ = ["step"]


def step(state: State, event: Event) -> tuple[State, tuple[Action, ...]]:
    match event:
        case BookUpdate():
            return _on_book_update(state, event)
        case Timer():
            # The background risk pass. It produces no Decision Records - an
            # evaluation nobody asked for would inflate the denominator.
            return sweep_stuck_entries(
                state.at_time(event.at_ms).with_republished_risk(), event.at_ms
            )
        case TunnelHealth():
            return _on_tunnel_health(state, event)
        case BalanceUpdate():
            return _on_balance_update(state, event)
        case OrderAck():
            # Recorded by the shell for reconciliation; the core has nothing to
            # decide until something fills or is refused.
            return state.at_time(event.at_ms), ()
        case Fill() | PartialFill():
            return _on_order_fill(state.at_time(event.at_ms), event)
        case Reject():
            return _on_order_reject(state.at_time(event.at_ms), event)
        case DisputeOpened():
            return trigger_exit(
                state.at_time(event.at_ms), event.pair_id, "dispute_opened", ""
            )
        case RuleDivergenceFound():
            return trigger_exit(
                state.at_time(event.at_ms),
                event.pair_id,
                "rule_divergence",
                event.detail,
            )
        case Postponement():
            return trigger_exit(
                state.at_time(event.at_ms), event.pair_id, "postponement", ""
            )
        case Settlement():
            return on_settlement(
                state.at_time(event.at_ms),
                event.pair_id,
                event.venue,
                event.payout_per_contract,
                event.at_ms,
            )
        case KillSwitch():
            return apply_kill_switch(state.at_time(event.at_ms), event.tier)


def _on_order_fill(
    state: State, event: Fill | PartialFill
) -> tuple[State, tuple[Action, ...]]:
    """Route by what the order was for.

    Entry orders belong to a `PendingEntry`; exit orders belong to an open
    `Position` and have no pending entry at all, so they need a separate path -
    routing everything through the entry handler is how exit fills came to be
    silently dropped.
    """
    ref = state.orders.get(event.order_id)
    if ref is None:
        return state, ()
    if ref.purpose == "exit":
        state = _retire(state, event.order_id)
        return on_exit_fill(
            state, ref.pair_id, ref.venue, event.size, event.price, event.at_ms
        )
    return on_fill(state, event.order_id, event.size, event.price, event.at_ms)


def _on_order_reject(state: State, event: Reject) -> tuple[State, tuple[Action, ...]]:
    ref = state.orders.get(event.order_id)
    if ref is None:
        return state, ()
    if ref.purpose == "exit":
        state = _retire(state, event.order_id)
        return on_exit_reject(state, ref.pair_id, ref.venue, event.at_ms)
    return on_reject(state, event.order_id)


def _retire(state: State, order_id: str) -> State:
    """Forget an order that has reported, so a duplicate event is a no-op."""
    return replace(
        state,
        orders={key: value for key, value in state.orders.items() if key != order_id},
    )


def _on_book_update(state: State, event: BookUpdate) -> tuple[State, tuple[Action, ...]]:
    state = state.with_book(event.snapshot).at_time(event.at_ms)

    # Only pairs with a leg on the contract that just moved are re-evaluated.
    # Every other pair's inputs are unchanged, so re-pricing them would write
    # duplicate Decision Records and distort the denominator.
    #
    # A pair already held or mid-entry is skipped entirely rather than recorded
    # as a rejection: it is not a candidate, and counting it would inflate the
    # denominator with evaluations that were never available to trade.
    records = [
        record
        for pair in state.pairs_touching(event.snapshot.key)
        if not _already_committed(state, pair.pair_id)
        if (record := evaluate_pair(state, pair, event.at_ms)) is not None
    ]

    actions: list[Action] = [EmitDecisionRecord(record) for record in records]

    accepted = rank_candidates(
        [record for record in records if record.accepted],
        venue_balances=state.venue_balances,
        limits=state.config.risk,
    )
    for record in accepted:
        state, entry_actions = _enter(state, record, event.at_ms)
        actions.extend(entry_actions)

    return state, tuple(actions)


def _enter(
    state: State, record: DecisionRecord, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """Turn an accepted Decision Record into leg 1.

    The walk is re-run rather than carried on the record because the record is
    an observation written for analysis, and threading execution state through
    it would make the decision log a control channel.
    """
    pair = state.pair_registry[record.pair_id]
    schedule = state.config.fees_for(pair.category)
    if schedule is None:
        return state, ()

    kalshi = state.books[pair.key_on("kalshi")]
    polymarket = state.books[pair.key_on("polymarket")]
    sized = walk(kalshi.asks, polymarket.asks, schedule)
    if not sized.is_tradeable:
        return state, ()

    return begin_entry(state, pair, sized, at_ms)


def _already_committed(state: State, pair_id: str) -> bool:
    return pair_id in state.pending or state.position_for(pair_id) is not None


def _on_tunnel_health(
    state: State, event: TunnelHealth
) -> tuple[State, tuple[Action, ...]]:
    updated = replace(
        state,
        venue_healthy={**state.venue_healthy, event.venue: event.healthy},
        venue_latency_ms={**state.venue_latency_ms, event.venue: event.latency_ms},
    )
    return updated.at_time(event.at_ms).with_republished_risk(), ()


def _on_balance_update(
    state: State, event: BalanceUpdate
) -> tuple[State, tuple[Action, ...]]:
    balances = {**state.venue_balances, event.venue: event.balance}
    updated = replace(state, venue_balances=balances)
    return updated.at_time(event.at_ms).with_republished_risk(), ()
