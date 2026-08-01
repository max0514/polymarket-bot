"""The target universe: which series are worth collecting at all.

Two filters, for two different reasons.

**Single-fact only.** The strategy needs outcomes so mechanically determinable
that every source reports them identically - sports results and scheduled
economic releases. Crypto is excluded by construction rather than by judgement:
Kalshi settles on CF Benchmarks and Polymarket on Chainlink, so a crypto pair
fails the matching rule before anyone looks at its price. Collecting it would
just fill the log with candidates that can never be approved.

**Series level, not market level.** Open-market counts on both venues are
dominated by combinatorially generated parlays - every scoreline, every
multi-leg combination - which have no cross-venue equivalent and would swamp
both the candidate queue and the subscription budget. Filtering at the series
level is what keeps the collector's bandwidth pointed at markets that could
actually pair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "ELIGIBLE_CATEGORIES",
    "Series",
    "SeriesFilter",
    "classify",
    "normalise_category",
]


@dataclass(frozen=True, slots=True)
class Series:
    """A candidate series as either venue advertises it."""

    series_ticker: str
    title: str
    venue_category: str | None = None

#: The only categories a pair can be approved in. Everything else is collected
#: by nobody, because it could not be traded even if it looked profitable.
ELIGIBLE_CATEGORIES: frozenset[str] = frozenset({"sports", "economics"})

_SPORTS_HINTS = (
    "nfl",
    "nba",
    "mlb",
    "nhl",
    "ncaa",
    "premier league",
    "world cup",
    "tennis",
    "golf",
    "ufc",
    "formula 1",
)

_ECONOMICS_HINTS = (
    "cpi",
    "ppi",
    "nonfarm",
    "payrolls",
    "unemployment",
    "fomc",
    "fed funds",
    "gdp",
    "jobless claims",
    "pce",
    "retail sales",
)

_CRYPTO_HINTS = (
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "solana",
    "sol",
    "dogecoin",
    "doge",
    "xrp",
    "crypto",
)

#: Wording that marks a combinatorial market rather than a single fact.
_PARLAY_HINTS = (
    "parlay",
    "exact score",
    "correct score",
    "both teams",
    "and also",
    " & ",
    "multi-leg",
    "same game",
)


def classify(title: str) -> str:
    """Best-effort category from a series title alone.

    Returns `"crypto"`, `"sports"`, `"economics"`, or `"other"`. Crypto is
    checked first: a title mentioning both Bitcoin and the CPI is a crypto
    market with an economic trigger, and it is still excluded.

    This is a *fallback*. Title keywords cannot recognise "Will the Chiefs
    win?" as sports without an unbounded list of team names, so
    `normalise_category` is preferred wherever the venue states a category
    itself - which both of them do.
    """
    text = title.casefold()
    if any(hint in text for hint in _CRYPTO_HINTS):
        return "crypto"
    if any(hint in text for hint in _SPORTS_HINTS):
        return "sports"
    if any(hint in text for hint in _ECONOMICS_HINTS):
        return "economics"
    return "other"


def normalise_category(venue_category: str) -> str:
    """Map a venue's own category label onto ours.

    Both venues tag their series, and their tag is authoritative in a way that
    a keyword scan of the title never is.
    """
    text = venue_category.casefold().strip()
    if "crypto" in text or "digital asset" in text:
        return "crypto"
    if "sport" in text:
        return "sports"
    if "econ" in text or "financial" in text:
        return "economics"
    return "other"


@dataclass(frozen=True, slots=True)
class SeriesFilter:
    """Decides which series the collector subscribes to."""

    #: Explicit allow-list of series tickers, checked before any heuristic.
    #: The heuristics are a bandwidth filter, not an approval mechanism - the
    #: Pair Registry is what actually gates trading - so an operator can always
    #: name a series directly.
    allowed_series: frozenset[str] = field(default_factory=frozenset)
    eligible_categories: frozenset[str] = ELIGIBLE_CATEGORIES

    def accepts(
        self, *, series_ticker: str, title: str, venue_category: str | None = None
    ) -> bool:
        if series_ticker in self.allowed_series:
            return True
        if _is_parlay(title):
            return False
        return self.category_of(title, venue_category) in self.eligible_categories

    def category_of(self, title: str, venue_category: str | None = None) -> str:
        """The venue's own label if it gave one, else the title heuristic."""
        if venue_category:
            resolved = normalise_category(venue_category)
            if resolved != "other":
                return resolved
        return classify(title)

    def select(self, series: Iterable[Series]) -> tuple[str, ...]:
        """Filter series, preserving input order."""
        return tuple(
            entry.series_ticker
            for entry in series
            if self.accepts(
                series_ticker=entry.series_ticker,
                title=entry.title,
                venue_category=entry.venue_category,
            )
        )


def _is_parlay(title: str) -> bool:
    text = title.casefold()
    return any(hint in text for hint in _PARLAY_HINTS)
