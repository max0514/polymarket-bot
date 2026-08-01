"""Core domain types.

Vocabulary follows the spec's glossary exactly: Venue, Series, Contract,
Matched Pair, Leg. Deviating here would make the code and the spec disagree
about what a word means, which is the failure the glossary exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, TypeAlias

__all__ = [
    "BookKey",
    "BookSnapshot",
    "Level",
    "MatchedPair",
    "VENUES",
    "Venue",
    "other_venue",
]

Venue: TypeAlias = Literal["kalshi", "polymarket"]

#: Both venues, in a fixed order. Iteration order reaches the action trace, so
#: it lives here once rather than being re-declared per module.
VENUES: tuple[Venue, ...] = ("kalshi", "polymarket")

#: Books are keyed by venue and contract, not by instrument file, so that
#: cross-pair analysis is a query rather than a join across files.
BookKey: TypeAlias = tuple[Venue, str]


def other_venue(venue: Venue) -> Venue:
    return "polymarket" if venue == "kalshi" else "kalshi"


@dataclass(frozen=True, slots=True)
class Level:
    """One price level. `size` is a contract count, always a whole number."""

    price: Decimal
    size: int


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """One venue's book for one contract at one moment.

    Carries both timestamps so freshness is measured rather than assumed
    (user story 20): `venue_time_ms` is the exchange's own clock,
    `received_time_ms` is when this process saw it.
    """

    venue: Venue
    contract_id: str
    asks: tuple[Level, ...]
    bids: tuple[Level, ...]
    venue_time_ms: int
    received_time_ms: int

    @property
    def key(self) -> BookKey:
        return (self.venue, self.contract_id)

    @property
    def best_ask(self) -> Level | None:
        """The price we buy at. Asks are ordered best (lowest) first."""
        return self.asks[0] if self.asks else None

    @property
    def best_bid(self) -> Level | None:
        """The price we sell at when unwinding. Best (highest) first."""
        return self.bids[0] if self.bids else None


@dataclass(frozen=True, slots=True)
class MatchedPair:
    """Two contracts, one per venue, verified to settle identically.

    Only pairs in the Pair Registry reach this type; candidates awaiting
    verification or approval are `PairCandidate` in `arb.registry`.
    """

    pair_id: str
    kalshi_contract_id: str
    polymarket_contract_id: str
    category: str
    settlement_source: str
    settlement_date: str

    def contract_on(self, venue: Venue) -> str:
        return (
            self.kalshi_contract_id
            if venue == "kalshi"
            else self.polymarket_contract_id
        )

    def key_on(self, venue: Venue) -> BookKey:
        return (venue, self.contract_on(venue))
