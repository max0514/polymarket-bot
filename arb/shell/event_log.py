"""Raw inbound event persistence.

Append-only JSONL, one event per line, in arrival order. This is what makes any
historical moment replayable exactly (user story 24) - and therefore what makes
the backtest and the regression suite the same artifact.

JSONL rather than SQLite because the access pattern is "write one, read all in
order, never update", and a file that can be read with `head` is easier to
trust than a database that has to be queried to be inspected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator

from arb.events import Event
from arb.replay import decode_event, encode_event

__all__ = ["EventLog"]


class EventLog:
    """Append-only log of everything the world told the reducer."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: Event) -> None:
        self.append_all([event])

    def append_all(self, events: Iterable[Event]) -> None:
        lines = [encode_event(event) for event in events]
        if not lines:
            return
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def __iter__(self) -> Iterator[Event]:
        """Every recorded event, in the order it arrived."""
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield decode_event(line)
