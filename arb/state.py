"""The reducer's state.

Immutable. Every transition returns a new `State` rather than mutating, so a
replayed event log cannot be perturbed by a caller holding a reference to an
earlier state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from arb.config import Config
from arb.domain import BookKey, BookSnapshot, MatchedPair

__all__ = ["State"]


@dataclass(frozen=True, slots=True)
class State:
    #: Configuration travels in state so a replay reproduces the original
    #: decisions rather than re-deciding under today's settings.
    config: Config = field(default_factory=Config)

    #: Latest book per (venue, contract).
    books: Mapping[BookKey, BookSnapshot] = field(default_factory=dict)

    #: The operator-approved set of Matched Pairs eligible for trading,
    #: published by the matching pipeline and read here as data.
    pair_registry: Mapping[str, MatchedPair] = field(default_factory=dict)

    #: Latest time the reducer has been told about. Advances only through
    #: events - never read from a clock.
    now_ms: int = 0

    def with_book(self, snapshot: BookSnapshot) -> State:
        return replace(self, books={**self.books, snapshot.key: snapshot})

    def at_time(self, at_ms: int) -> State:
        """Advance the known time. Never moves backwards, so an out-of-order
        event cannot make a fresh book look stale."""
        return replace(self, now_ms=max(self.now_ms, at_ms))

    def pairs_touching(self, key: BookKey) -> tuple[MatchedPair, ...]:
        """Registered pairs with a leg on this contract, in a stable order.

        Sorted by pair id because iteration order reaches the action trace, and
        an action trace that depends on dict insertion order is not replayable.
        """
        return tuple(
            self.pair_registry[pair_id]
            for pair_id in sorted(self.pair_registry)
            if key in (
                self.pair_registry[pair_id].key_on("kalshi"),
                self.pair_registry[pair_id].key_on("polymarket"),
            )
        )
