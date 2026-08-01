"""Events - everything the world tells the reducer.

`Timer` and `TunnelHealth` are events rather than ambient reads. That is the
decision that makes replay deterministic: the reducer never asks what time it
is or whether a socket is up, it is told.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias

from arb.domain import BookSnapshot, Venue

__all__ = [
    "BalanceUpdate",
    "BookUpdate",
    "Event",
    "Fill",
    "OrderAck",
    "PartialFill",
    "Reject",
    "Timer",
    "TunnelHealth",
]


@dataclass(frozen=True, slots=True)
class BookUpdate:
    """A fresh book for one contract on one venue."""

    snapshot: BookSnapshot

    @property
    def at_ms(self) -> int:
        return self.snapshot.received_time_ms


@dataclass(frozen=True, slots=True)
class Timer:
    """The passage of time, delivered rather than read.

    Staleness is measured against the reducer's latest known time, which only
    advances through events - so a book that stops updating goes stale only
    when something else tells the reducer that time has moved.
    """

    at_ms: int


@dataclass(frozen=True, slots=True)
class TunnelHealth:
    """Per-venue connectivity, surfaced continuously so that a degraded link is
    visible before it causes a Leg Failure (user story 23).

    Carries measured round-trip latency because leg difficulty is scored from
    it - "harder" has to be measured rather than assumed.
    """

    venue: Venue
    healthy: bool
    at_ms: int
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class OrderAck:
    """The venue acknowledged an order. Recorded for reconciliation."""

    order_id: str
    at_ms: int


@dataclass(frozen=True, slots=True)
class Fill:
    """An order filled completely, at the price it actually got."""

    order_id: str
    size: int
    price: Decimal
    at_ms: int


@dataclass(frozen=True, slots=True)
class PartialFill:
    """An order filled for less than it asked. `size` is what actually filled."""

    order_id: str
    size: int
    price: Decimal
    at_ms: int


@dataclass(frozen=True, slots=True)
class Reject:
    """The venue refused the order, or it could not fill inside its limit."""

    order_id: str
    reason: str
    at_ms: int


@dataclass(frozen=True, slots=True)
class BalanceUpdate:
    """A venue's settled cash balance.

    Balances are a strategy input, not just a safety check: both legs are paid
    on entry but the payout lands entirely at whichever venue holds the winning
    side, so per-venue balance is what Drift accumulates in.
    """

    venue: Venue
    balance: Decimal
    at_ms: int


Event: TypeAlias = (
    BookUpdate
    | Timer
    | TunnelHealth
    | BalanceUpdate
    | OrderAck
    | Fill
    | PartialFill
    | Reject
)
