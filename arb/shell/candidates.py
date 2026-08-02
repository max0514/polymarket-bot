"""Candidate persistence - the approval workflow's memory.

Unlike the Decision Record store, this one is *not* append-only: a candidate is
a thing with a lifecycle (proposed, verified, decided, eventually labelled by
settlement), and each stage overwrites the last. What must not be lost is the
history recorded *on* the row - model confidence, rule verdict, operator
decision, ground truth - because together they are the calibration dataset.

Contract terms are stored as JSON rather than flattened into columns. Every
term is nullable, and `NULL` has to survive the round trip distinctly from the
empty string: that difference is exactly "the venue did not state this" versus
"the venue stated nothing", which is the difference between unverifiable and
matched.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping

from arb.domain import MatchedPair
from arb.registry import PairCandidate, PairStatus, registry_from
from arb.verification import ContractTerms, KalshiSeries, PolymarketMarket

__all__ = ["CandidateStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pair_candidates (
    pair_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    settlement_date TEXT NOT NULL,
    model_confidence TEXT NOT NULL,
    proposed_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    kalshi_ticker TEXT NOT NULL,
    kalshi_title TEXT NOT NULL,
    kalshi_url TEXT NOT NULL,
    kalshi_terms TEXT NOT NULL,
    polymarket_id TEXT NOT NULL,
    polymarket_question TEXT NOT NULL,
    polymarket_url TEXT NOT NULL,
    polymarket_terms TEXT NOT NULL,
    operator TEXT NOT NULL DEFAULT '',
    operator_note TEXT NOT NULL DEFAULT '',
    decided_at_ms INTEGER,
    settled_identically INTEGER
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON pair_candidates(status);
"""

_TERM_FIELDS = (
    "settlement_source",
    "settling_release",
    "settling_release_timestamp",
    "void_rule",
    "postponement_rule",
    "overtime_rule",
    "threshold",
    "tie_break_rule",
)


class CandidateStore:
    """Every proposed pair and whatever has happened to it since."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save(self, candidate: PairCandidate) -> None:
        """Insert or replace. A candidate has one row for its whole life."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pair_candidates (
                    pair_id, category, settlement_date, model_confidence,
                    proposed_at_ms, status,
                    kalshi_ticker, kalshi_title, kalshi_url, kalshi_terms,
                    polymarket_id, polymarket_question, polymarket_url,
                    polymarket_terms,
                    operator, operator_note, decided_at_ms, settled_identically
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.pair_id,
                    candidate.category,
                    candidate.settlement_date,
                    str(candidate.model_confidence),
                    candidate.proposed_at_ms,
                    candidate.status.value,
                    candidate.kalshi.series_ticker,
                    candidate.kalshi.title,
                    candidate.kalshi.contract_terms_url,
                    _terms_to_json(candidate.kalshi.terms),
                    candidate.polymarket.condition_id,
                    candidate.polymarket.question,
                    candidate.polymarket.resolution_source_url,
                    _terms_to_json(candidate.polymarket.terms),
                    candidate.operator,
                    candidate.operator_note,
                    candidate.decided_at_ms,
                    _flag(candidate.settled_identically),
                ),
            )

    def get(self, pair_id: str) -> PairCandidate | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pair_candidates WHERE pair_id = ?", (pair_id,)
            ).fetchone()
        return _from_row(row) if row is not None else None

    def all(self) -> list[PairCandidate]:
        """Every candidate, ordered by id so the review queue is stable."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pair_candidates ORDER BY pair_id"
            ).fetchall()
        return [_from_row(row) for row in rows]

    def registry(self) -> Mapping[str, MatchedPair]:
        """The Pair Registry the reducer reads: approved candidates only."""
        return registry_from(self.all())


def _terms_to_json(terms: ContractTerms) -> str:
    payload: dict[str, Any] = {field: getattr(terms, field) for field in _TERM_FIELDS}
    payload["revisable"] = terms.revisable
    return json.dumps(payload, sort_keys=True)


def _terms_from_json(raw: str) -> ContractTerms:
    payload = json.loads(raw)
    return ContractTerms(
        revisable=bool(payload["revisable"]),
        **{field: payload.get(field) for field in _TERM_FIELDS},
    )


def _from_row(row: sqlite3.Row) -> PairCandidate:
    return PairCandidate(
        pair_id=row["pair_id"],
        kalshi=KalshiSeries(
            series_ticker=row["kalshi_ticker"],
            title=row["kalshi_title"],
            contract_terms_url=row["kalshi_url"],
            terms=_terms_from_json(row["kalshi_terms"]),
        ),
        polymarket=PolymarketMarket(
            condition_id=row["polymarket_id"],
            question=row["polymarket_question"],
            resolution_source_url=row["polymarket_url"],
            terms=_terms_from_json(row["polymarket_terms"]),
        ),
        category=row["category"],
        settlement_date=row["settlement_date"],
        model_confidence=Decimal(row["model_confidence"]),
        proposed_at_ms=row["proposed_at_ms"],
        status=PairStatus(row["status"]),
        operator=row["operator"],
        operator_note=row["operator_note"],
        decided_at_ms=row["decided_at_ms"],
        settled_identically=_unflag(row["settled_identically"]),
    )


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _unflag(value: int | None) -> bool | None:
    return None if value is None else bool(value)
