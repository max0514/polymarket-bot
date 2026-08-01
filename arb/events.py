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
    "BookUpdate",
    "Event",
    "Timer",
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


Event: TypeAlias = BookUpdate | Timer
