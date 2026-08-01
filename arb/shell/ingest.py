"""Ingestion: venue books to `BookUpdate` events.

A `BookSource` yields raw `(venue, contract_id, payload, received_time_ms)`
tuples from wherever books come from. `IngestionPipeline` normalises them,
drops anything outside the target universe, and hands `BookUpdate` events to
the runtime.

The source is a seam for a reason. Books arrive over a venue websocket in
production, from a recorded log in a backtest, and from a fixture in a test,
and none of that should be visible to anything downstream of this module.

**Subscription, not polling.** The spec requires book updates by subscription
so that freshness does not degrade as the pair count grows - a poller's
round-trip time is a function of how many markets it is watching, which is
exactly the wrong dependency for a system whose Skew gate rejects stale books.
A `BookSource` is therefore modelled as a stream that pushes, not a function
that is called on a timer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Protocol

from arb.domain import BookSnapshot, Venue
from arb.events import BookUpdate
from arb.shell.normalise import kalshi_snapshot, polymarket_snapshot
from arb.shell.runtime import Runtime

__all__ = ["BookMessage", "BookSource", "IngestionPipeline", "RecordedSource"]


@dataclass(frozen=True, slots=True)
class BookMessage:
    """One raw book, as the venue sent it."""

    venue: Venue
    contract_id: str
    payload: dict[str, Any]
    #: Stamped by us on arrival. The only clock both venues can be compared on.
    received_time_ms: int


class BookSource(Protocol):
    """A stream of raw books. Implementations subscribe; they do not poll."""

    def __iter__(self) -> Iterator[BookMessage]: ...


@dataclass(frozen=True, slots=True)
class RecordedSource:
    """A source backed by a fixed list - for tests and for replaying fixtures."""

    messages: tuple[BookMessage, ...]

    def __iter__(self) -> Iterator[BookMessage]:
        return iter(self.messages)


class IngestionPipeline:
    """Normalise inbound books and feed them to the runtime.

    Contracts not in the Pair Registry are dropped here rather than in the
    reducer. The reducer would ignore them anyway, but writing them to the
    event log would fill it with books nothing can ever trade against - and
    the log's value is that it replays a decision, not that it archives a feed.
    """

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def feed(self, source: BookSource | Iterable[BookMessage]) -> int:
        """Consume a source. Returns how many messages were accepted."""
        accepted = 0
        for message in source:
            if self.accept(message):
                accepted += 1
        return accepted

    def accept(self, message: BookMessage) -> bool:
        snapshot = normalise(message)
        if snapshot.key not in self._tradeable_keys():
            return False
        self._runtime.handle(BookUpdate(snapshot))
        return True

    def _tradeable_keys(self) -> frozenset[tuple[str, str]]:
        registry = self._runtime.state.pair_registry
        return frozenset(
            key
            for pair in registry.values()
            for key in (pair.key_on("kalshi"), pair.key_on("polymarket"))
        )


def normalise(message: BookMessage) -> BookSnapshot:
    """Route a raw book to its venue's normaliser."""
    if message.venue == "kalshi":
        return kalshi_snapshot(
            message.payload,
            contract_id=message.contract_id,
            received_time_ms=message.received_time_ms,
        )
    return polymarket_snapshot(
        message.payload,
        contract_id=message.contract_id,
        received_time_ms=message.received_time_ms,
    )
