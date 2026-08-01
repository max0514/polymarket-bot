"""Deterministic replay: the regression suite and the backtest, in one artifact.

A recorded event log fed back through the reducer must produce a byte-identical
action trace. That single property is doing a lot of work:

* It is the regression test. A changed trace means changed logic, and nothing
  else - not a different machine, not a different hour, not dictionary
  ordering.
* It is the backtest. The same code path that trades produces the historical
  decisions, so a backtest cannot flatter the strategy by simulating it
  differently from how it runs.
* It protects the verdict. The decision log is the deliverable, and a log that
  cannot be regenerated from its inputs is an assertion rather than evidence.

Determinism is why `Timer` and `TunnelHealth` are events rather than ambient
reads, why order ids are minted from a state counter, why every mapping the
reducer iterates is sorted, and why decimals serialise through
`canonical_decimal`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from arb.actions import (
    Action,
    Alert,
    CancelOrder,
    EmitDecisionRecord,
    EmitExitRecord,
    EmitSettlementRecord,
    PlaceOrder,
)
from arb.canonical import canonical_decimal
from arb.domain import BookSnapshot, Level
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
from arb.reducer import step
from arb.state import State

__all__ = [
    "action_trace",
    "decode_event",
    "describe_action",
    "encode_event",
    "replay",
]


def replay(state: State, events: Iterable[Event]) -> tuple[State, tuple[Action, ...]]:
    """Fold a recorded event log through the reducer."""
    actions: list[Action] = []
    for event in events:
        state, produced = step(state, event)
        actions.extend(produced)
    return state, tuple(actions)


def action_trace(actions: Sequence[Action]) -> tuple[str, ...]:
    """The canonical text form of an action trace, one line per action."""
    return tuple(describe_action(action) for action in actions)


def describe_action(action: Action) -> str:
    """One action as one deterministic line.

    Deliberately lossy: it records the decisions a reader would want to diff -
    what was ordered, what was recorded, what was alerted - not every field.
    A trace that included, say, book depth at the moment of the decision would
    change whenever an unrelated level moved, and would stop being a useful
    regression signal.
    """
    match action:
        case EmitDecisionRecord():
            record = action.record
            reason = record.rejection_reason.value if record.rejection_reason else "-"
            return (
                f"decision {record.pair_id} "
                f"{'accept' if record.accepted else 'reject'} {reason} "
                f"net={_decimal(record.net_edge)} size={record.size}"
            )
        case EmitSettlementRecord():
            settled = action.record
            return (
                f"settlement {settled.pair_id} "
                f"realised={canonical_decimal(settled.realised_profit)} "
                f"mismatch={'1' if settled.mismatch else '0'}"
            )
        case EmitExitRecord():
            closed = action.record
            return (
                f"exit {closed.pair_id} {closed.trigger or '-'} "
                f"realised={canonical_decimal(closed.realised_profit)} "
                f"unsold={','.join(closed.legs_unsold) or '-'}"
            )
        case PlaceOrder():
            return (
                f"order {action.order_id} {action.purpose} {action.venue} "
                f"{action.side} {action.size} @ {canonical_decimal(action.limit_price)}"
            )
        case CancelOrder():
            return f"cancel {action.order_id} {action.venue}"
        case Alert():
            return f"alert {action.severity} {action.pair_id or '-'} {action.message}"


def encode_event(event: Event) -> str:
    """One event as one JSON line.

    Keys are sorted and decimals are canonical strings, so the same event
    always produces the same bytes.
    """
    return json.dumps(_encode(event), sort_keys=True, separators=(",", ":"))


def decode_event(line: str) -> Event:
    payload = json.loads(line)
    kind = payload.pop("type")
    builder = _DECODERS.get(kind)
    if builder is None:
        raise ValueError(f"unknown event type in log: {kind!r}")
    event: Event = builder(payload)
    return event


def _encode(event: Event) -> dict[str, Any]:
    match event:
        case BookUpdate():
            return {"type": "book_update", "snapshot": _encode_snapshot(event.snapshot)}
        case Timer():
            return {"type": "timer", "at_ms": event.at_ms}
        case TunnelHealth():
            return {
                "type": "tunnel_health",
                "venue": event.venue,
                "healthy": event.healthy,
                "at_ms": event.at_ms,
                "latency_ms": event.latency_ms,
            }
        case BalanceUpdate():
            return {
                "type": "balance_update",
                "venue": event.venue,
                "balance": canonical_decimal(event.balance),
                "at_ms": event.at_ms,
            }
        case OrderAck():
            return {"type": "order_ack", "order_id": event.order_id, "at_ms": event.at_ms}
        case Fill() | PartialFill():
            return {
                "type": "fill" if isinstance(event, Fill) else "partial_fill",
                "order_id": event.order_id,
                "size": event.size,
                "price": canonical_decimal(event.price),
                "at_ms": event.at_ms,
            }
        case Reject():
            return {
                "type": "reject",
                "order_id": event.order_id,
                "reason": event.reason,
                "at_ms": event.at_ms,
            }
        case DisputeOpened():
            return {
                "type": "dispute_opened",
                "pair_id": event.pair_id,
                "at_ms": event.at_ms,
            }
        case RuleDivergenceFound():
            return {
                "type": "rule_divergence",
                "pair_id": event.pair_id,
                "detail": event.detail,
                "at_ms": event.at_ms,
            }
        case Postponement():
            return {
                "type": "postponement",
                "pair_id": event.pair_id,
                "at_ms": event.at_ms,
            }
        case Settlement():
            return {
                "type": "settlement",
                "pair_id": event.pair_id,
                "venue": event.venue,
                "payout_per_contract": canonical_decimal(event.payout_per_contract),
                "at_ms": event.at_ms,
            }
        case KillSwitch():
            return {"type": "kill_switch", "tier": event.tier, "at_ms": event.at_ms}


def _encode_snapshot(snapshot: BookSnapshot) -> dict[str, Any]:
    return {
        "venue": snapshot.venue,
        "contract_id": snapshot.contract_id,
        "asks": [_encode_level(level) for level in snapshot.asks],
        "bids": [_encode_level(level) for level in snapshot.bids],
        "venue_time_ms": snapshot.venue_time_ms,
        "received_time_ms": snapshot.received_time_ms,
    }


def _encode_level(level: Level) -> list[Any]:
    return [canonical_decimal(level.price), level.size]


def _decode_snapshot(payload: dict[str, Any]) -> BookSnapshot:
    return BookSnapshot(
        venue=payload["venue"],
        contract_id=payload["contract_id"],
        asks=tuple(Level(Decimal(p), s) for p, s in payload["asks"]),
        bids=tuple(Level(Decimal(p), s) for p, s in payload["bids"]),
        venue_time_ms=payload["venue_time_ms"],
        received_time_ms=payload["received_time_ms"],
    )


_DECODERS: dict[str, Callable[[dict[str, Any]], Event]] = {
    "book_update": lambda p: BookUpdate(_decode_snapshot(p["snapshot"])),
    "timer": lambda p: Timer(p["at_ms"]),
    "tunnel_health": lambda p: TunnelHealth(
        p["venue"], p["healthy"], p["at_ms"], p["latency_ms"]
    ),
    "balance_update": lambda p: BalanceUpdate(
        p["venue"], Decimal(p["balance"]), p["at_ms"]
    ),
    "order_ack": lambda p: OrderAck(p["order_id"], p["at_ms"]),
    "fill": lambda p: Fill(p["order_id"], p["size"], Decimal(p["price"]), p["at_ms"]),
    "partial_fill": lambda p: PartialFill(
        p["order_id"], p["size"], Decimal(p["price"]), p["at_ms"]
    ),
    "reject": lambda p: Reject(p["order_id"], p["reason"], p["at_ms"]),
    "dispute_opened": lambda p: DisputeOpened(p["pair_id"], p["at_ms"]),
    "rule_divergence": lambda p: RuleDivergenceFound(
        p["pair_id"], p["detail"], p["at_ms"]
    ),
    "postponement": lambda p: Postponement(p["pair_id"], p["at_ms"]),
    "settlement": lambda p: Settlement(
        p["pair_id"], p["venue"], Decimal(p["payout_per_contract"]), p["at_ms"]
    ),
    "kill_switch": lambda p: KillSwitch(p["tier"], p["at_ms"]),
}


def _decimal(value: Decimal | None) -> str:
    return canonical_decimal(value) if value is not None else "-"
