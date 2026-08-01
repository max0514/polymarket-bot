"""Test builders.

Constructing a full `State` or `BookSnapshot` inline makes tests unreadable and
couples every test to fields it does not care about. These builders default
everything irrelevant so each test names only what it is actually about.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from arb.actions import Action, EmitDecisionRecord
from arb.config import Config
from arb.decisions import DecisionRecord
from arb.domain import BookKey, BookSnapshot, Level, MatchedPair, Venue
from arb.pricing import FeeSchedule
from arb.risk import RiskLimits
from arb.state import KillTier, Position, State

SPORTS_FEES = FeeSchedule(kalshi_rate=Decimal("0.07"), polymarket_rate=Decimal("0.05"))

PAIR_ID = "nfl-2026-w1-kc-win"

Levels = Sequence[tuple[str, int]]


def config(
    *,
    fee_schedules: Mapping[str, FeeSchedule] | None = None,
    min_net_edge: Decimal = Decimal("0.002"),
    max_skew_ms: int = 2_000,
    max_book_age_ms: int = 5_000,
    entry_timeout_ms: int = 30_000,
    limits: RiskLimits | None = None,
) -> Config:
    return Config(
        fee_schedules=(
            {"sports": SPORTS_FEES, "economics": SPORTS_FEES}
            if fee_schedules is None
            else fee_schedules
        ),
        min_net_edge=min_net_edge,
        max_skew_ms=max_skew_ms,
        max_book_age_ms=max_book_age_ms,
        entry_timeout_ms=entry_timeout_ms,
        risk=RiskLimits() if limits is None else limits,
    )


def pair(
    pair_id: str = PAIR_ID,
    *,
    category: str = "sports",
    settlement_source: str = "NFL official box score",
    settlement_date: str = "2026-09-10",
) -> MatchedPair:
    return MatchedPair(
        pair_id=pair_id,
        kalshi_contract_id=f"{pair_id}-K",
        polymarket_contract_id=f"{pair_id}-P",
        category=category,
        settlement_source=settlement_source,
        settlement_date=settlement_date,
    )


def levels(raw: Iterable[tuple[str, int]]) -> tuple[Level, ...]:
    return tuple(Level(Decimal(price), size) for price, size in raw)


def book(
    venue: Venue,
    contract_id: str,
    *,
    asks: Levels = (("0.10", 500),),
    bids: Levels = (("0.09", 500),),
    venue_time_ms: int = 1_000_000,
    received_time_ms: int | None = None,
) -> BookSnapshot:
    return BookSnapshot(
        venue=venue,
        contract_id=contract_id,
        asks=levels(asks),
        bids=levels(bids),
        venue_time_ms=venue_time_ms,
        received_time_ms=(
            venue_time_ms if received_time_ms is None else received_time_ms
        ),
    )


def kalshi_book(
    matched: MatchedPair,
    *,
    asks: Levels = (("0.10", 500),),
    bids: Levels = (("0.09", 500),),
    venue_time_ms: int = 1_000_000,
    received_time_ms: int | None = None,
) -> BookSnapshot:
    return book(
        "kalshi",
        matched.kalshi_contract_id,
        asks=asks,
        bids=bids,
        venue_time_ms=venue_time_ms,
        received_time_ms=received_time_ms,
    )


def polymarket_book(
    matched: MatchedPair,
    *,
    asks: Levels = (("0.88", 500),),
    bids: Levels = (("0.87", 500),),
    venue_time_ms: int = 1_000_000,
    received_time_ms: int | None = None,
) -> BookSnapshot:
    return book(
        "polymarket",
        matched.polymarket_contract_id,
        asks=asks,
        bids=bids,
        venue_time_ms=venue_time_ms,
        received_time_ms=received_time_ms,
    )


def state_with(
    *pairs: MatchedPair,
    config_: Config | None = None,
    books: Mapping[BookKey, BookSnapshot] | None = None,
    positions: tuple[Position, ...] = (),
    leg_failures: int = 0,
    kill_tier: KillTier = KillTier.NONE,
    now_ms: int = 0,
) -> State:
    """A state whose registry holds the given pairs."""
    return State(
        config=config() if config_ is None else config_,
        pair_registry={matched.pair_id: matched for matched in pairs},
        books={} if books is None else books,
        positions=positions,
        leg_failures=leg_failures,
        kill_tier=kill_tier,
        now_ms=now_ms,
    )


def records(actions: tuple[Action, ...]) -> list[DecisionRecord]:
    """The Decision Records in an action trace."""
    return [a.record for a in actions if isinstance(a, EmitDecisionRecord)]
