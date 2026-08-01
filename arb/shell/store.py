"""Decision Record persistence.

One table, keyed for cross-pair analysis rather than split into per-instrument
files, because every question the verdict asks - base rate by price band, by
category, by rejection reason - is a query across pairs.

Rows are the flat string form produced by `DecisionRecord.as_record()`. Storing
canonical strings rather than SQLite REALs keeps the stored value identical to
the value the reducer computed; a float round-trip would not.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from arb.actions import PlaceOrder
from arb.canonical import canonical_decimal
from arb.decisions import DecisionRecord

__all__ = ["COLUMNS", "DecisionStore", "OrderStore"]

#: Column order is fixed and explicit. A schema derived from whatever keys the
#: first record happened to carry would drift silently as the record grows.
COLUMNS: tuple[str, ...] = (
    "pair_id",
    "category",
    "settlement_source",
    "settlement_date",
    "evaluated_at_ms",
    "accepted",
    "rejection_reason",
    "kalshi_price",
    "polymarket_price",
    "gross_edge",
    "net_edge",
    "expected_profit",
    "skew_ms",
    "kalshi_book_age_ms",
    "polymarket_book_age_ms",
    "kalshi_top_size",
    "polymarket_top_size",
    "size",
    "blocking_flags",
    "fee_kalshi_rate",
    "fee_polymarket_rate",
    "fee_kalshi_fee",
    "fee_polymarket_fee",
    "fee_total",
)

_COLUMN_DDL = ",\n    ".join(f"{column} TEXT NOT NULL DEFAULT ''" for column in COLUMNS)

_ORDER_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    side TEXT NOT NULL,
    purpose TEXT NOT NULL,
    size INTEGER NOT NULL,
    limit_price TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_pair ON order_attempts(pair_id);
CREATE INDEX IF NOT EXISTS idx_orders_order_id ON order_attempts(order_id);
"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS decision_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {_COLUMN_DDL}
);
CREATE INDEX IF NOT EXISTS idx_decisions_pair ON decision_records(pair_id);
CREATE INDEX IF NOT EXISTS idx_decisions_reason ON decision_records(rejection_reason);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON decision_records(evaluated_at_ms);
CREATE INDEX IF NOT EXISTS idx_decisions_category ON decision_records(category);
"""


class _SqliteStore:
    """Shared plumbing: one file, one schema, one connection discipline."""

    _schema: str = ""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(self._schema)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on error, and always close.

        `with sqlite3.connect(...)` alone commits but never closes, which leaks
        a handle per append - and these stores are appended to on every book
        update.
        """
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()


class DecisionStore(_SqliteStore):
    """Append-only log of evaluations.

    Append-only on purpose: a Decision Record is an observation of what the
    system decided at a moment, and an observation that can be edited is not
    evidence.
    """

    _schema = _SCHEMA

    def append(self, record: DecisionRecord) -> None:
        self.append_all([record])

    def append_all(self, records: Iterable[DecisionRecord]) -> None:
        rows = [
            tuple(record.as_record()[column] for column in COLUMNS)
            for record in records
        ]
        if not rows:
            return
        placeholders = ", ".join("?" * len(COLUMNS))
        with self._connect() as conn:
            conn.executemany(
                f"INSERT INTO decision_records ({', '.join(COLUMNS)}) "
                f"VALUES ({placeholders})",
                rows,
            )

    def all(self) -> list[dict[str, str]]:
        """Every record, in the order it was written."""
        with self._connect() as conn:
            cursor = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM decision_records ORDER BY id"
            )
            return [dict(row) for row in cursor.fetchall()]

    def count_by_reason(self) -> dict[str, int]:
        """The funnel: how many candidates each gate removed.

        The empty-string key counts accepted candidates.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT rejection_reason, COUNT(*) AS n "
                "FROM decision_records GROUP BY rejection_reason"
            )
            return {row["rejection_reason"]: row["n"] for row in cursor.fetchall()}


class OrderStore(_SqliteStore):
    """Every order this system decided to send.

    Acknowledgements, fills and rejections arrive as events and are already in
    the event log, so recording the attempt here is what completes the round
    trip: realised execution can be reconciled against intent even when the
    attempt never became a fill.
    """

    _schema = _ORDER_SCHEMA

    def append_all(self, orders: Iterable[PlaceOrder]) -> None:
        rows = [
            (
                order.order_id,
                order.pair_id,
                order.venue,
                order.contract_id,
                order.side,
                order.purpose,
                order.size,
                canonical_decimal(order.limit_price),
            )
            for order in orders
        ]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO order_attempts (order_id, pair_id, venue, contract_id, "
                "side, purpose, size, limit_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def all(self) -> list[dict[str, Any]]:
        """Mixed-typed rows: `size` is a genuine integer, everything else text."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT order_id, pair_id, venue, contract_id, side, purpose, size, "
                "limit_price FROM order_attempts ORDER BY id"
            )
            return [dict(row) for row in cursor.fetchall()]
