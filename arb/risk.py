"""Risk flags and budgets, computed off the execution hot path.

Everything here runs on the background clock - `Timer`, `TunnelHealth`,
`BalanceUpdate`, and position changes - and publishes its conclusions onto
`State`. The execution path reads a set of flags and a table of remaining
budgets. It never calls into this module, because the window between leg 1 and
leg 2 is the one place where computation time turns into money.

Two monitors the spec explicitly removed are absent: divergence probability and
market volatility. Both are convergence-trading metrics, and this system holds
to settlement, where post-entry price movement is irrelevant to a locked pair.
Volatility survives only as an input to the leg-2 breakeven bound in
`arb.execution`, where it sizes the exposure window.

Account-level risk is treated as a top-line term rather than a tail: the
connectivity flags block entry outright rather than discounting an expected
value, because an account that cannot reach a venue cannot close a position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Iterable, Mapping, Protocol

from arb.domain import MatchedPair, Venue

__all__ = [
    "HasExposure",
    "RiskBudgets",
    "RiskFlag",
    "RiskLimits",
    "blocking_flags_for",
    "evaluate_risk",
]

#: Large enough to be no limit at all. The spec proposes no defaults because
#: capital was never specified, so an unconfigured system imposes no cap rather
#: than inventing one that looks authoritative.
UNLIMITED = Decimal("1e18")


class RiskFlag(Enum):
    """A precomputed boolean the execution path reads without blocking."""

    KALSHI_CONNECTIVITY_DEGRADED = "kalshi_connectivity_degraded"
    POLYMARKET_CONNECTIVITY_DEGRADED = "polymarket_connectivity_degraded"
    KALSHI_BALANCE_FLOOR = "kalshi_balance_floor"
    POLYMARKET_BALANCE_FLOOR = "polymarket_balance_floor"
    LEG_FAILURE_BUDGET = "leg_failure_budget"
    UNSETTLED_CAPITAL_CAP = "unsettled_capital_cap"

    #: Raised per candidate rather than globally - see `blocking_flags_for`.
    SOURCE_CONCENTRATION = "source_concentration"
    CATEGORY_CONCENTRATION = "category_concentration"
    SETTLEMENT_DATE_CONCENTRATION = "settlement_date_concentration"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Configuration. Every limit defaults to no limit."""

    balance_floor: Decimal = Decimal("0")
    max_exposure_per_source: Decimal = UNLIMITED
    max_exposure_per_category: Decimal = UNLIMITED
    max_exposure_per_settlement_date: Decimal = UNLIMITED
    max_unsettled_capital: Decimal = UNLIMITED
    leg_failure_budget: int = 1_000_000

    #: Balance below which pair selection starts steering capital Drift toward
    #: the depleted venue, at the cost of some profit. Zero means never steer -
    #: the spec proposes no default because total capital was never specified.
    steering_balance_band: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class RiskBudgets:
    """How much room is left, published for the execution path to read."""

    per_source_remaining: Mapping[str, Decimal] = field(default_factory=dict)
    per_category_remaining: Mapping[str, Decimal] = field(default_factory=dict)
    per_settlement_date_remaining: Mapping[str, Decimal] = field(default_factory=dict)
    unsettled_capital_remaining: Decimal = UNLIMITED
    leg_failures_remaining: int = 1_000_000

    def source_room(self, source: str, limit: Decimal) -> Decimal:
        return self.per_source_remaining.get(source, limit)

    def category_room(self, category: str, limit: Decimal) -> Decimal:
        return self.per_category_remaining.get(category, limit)

    def settlement_date_room(self, date: str, limit: Decimal) -> Decimal:
        return self.per_settlement_date_remaining.get(date, limit)


class _Exposure:
    """Notional currently committed, sliced along each concentration axis."""

    def __init__(self, positions: Iterable[HasExposure]) -> None:
        self.by_source: dict[str, Decimal] = {}
        self.by_category: dict[str, Decimal] = {}
        self.by_settlement_date: dict[str, Decimal] = {}
        self.total = Decimal("0")
        for item in positions:
            notional = item.notional
            self.total += notional
            _add(self.by_source, item.settlement_source, notional)
            _add(self.by_category, item.category, notional)
            _add(self.by_settlement_date, item.settlement_date, notional)


class HasExposure(Protocol):
    """What `evaluate_risk` needs from a position.

    Structural rather than a concrete import so that `arb.risk` does not depend
    on `arb.state`, which depends on it.
    """

    @property
    def settlement_source(self) -> str: ...

    @property
    def category(self) -> str: ...

    @property
    def settlement_date(self) -> str: ...

    @property
    def notional(self) -> Decimal: ...


def evaluate_risk(
    *,
    limits: RiskLimits,
    positions: Iterable[HasExposure],
    venue_balances: Mapping[Venue, Decimal],
    venue_healthy: Mapping[Venue, bool],
    leg_failures: int,
) -> tuple[frozenset[RiskFlag], RiskBudgets]:
    """The whole background pass: flags plus remaining budgets."""
    exposure = _Exposure(positions)
    flags: set[RiskFlag] = set()

    if not venue_healthy.get("kalshi", True):
        flags.add(RiskFlag.KALSHI_CONNECTIVITY_DEGRADED)
    if not venue_healthy.get("polymarket", True):
        flags.add(RiskFlag.POLYMARKET_CONNECTIVITY_DEGRADED)

    # An unreported balance is not assumed healthy: a venue that has never told
    # us what it holds is a venue we cannot size against.
    if venue_balances.get("kalshi", Decimal("0")) < limits.balance_floor:
        flags.add(RiskFlag.KALSHI_BALANCE_FLOOR)
    if venue_balances.get("polymarket", Decimal("0")) < limits.balance_floor:
        flags.add(RiskFlag.POLYMARKET_BALANCE_FLOOR)

    if leg_failures >= limits.leg_failure_budget:
        flags.add(RiskFlag.LEG_FAILURE_BUDGET)

    unsettled_remaining = limits.max_unsettled_capital - exposure.total
    if unsettled_remaining <= 0:
        flags.add(RiskFlag.UNSETTLED_CAPITAL_CAP)

    budgets = RiskBudgets(
        per_source_remaining=_remaining(exposure.by_source, limits.max_exposure_per_source),
        per_category_remaining=_remaining(
            exposure.by_category, limits.max_exposure_per_category
        ),
        per_settlement_date_remaining=_remaining(
            exposure.by_settlement_date, limits.max_exposure_per_settlement_date
        ),
        unsettled_capital_remaining=unsettled_remaining,
        leg_failures_remaining=max(0, limits.leg_failure_budget - leg_failures),
    )
    return frozenset(flags), budgets


def blocking_flags_for(
    pair: MatchedPair,
    required_notional: Decimal,
    *,
    flags: frozenset[RiskFlag],
    budgets: RiskBudgets,
    limits: RiskLimits,
) -> tuple[RiskFlag, ...]:
    """Every flag that blocks *this* candidate, in a stable order.

    Global flags apply to any candidate. The concentration checks are
    per-candidate by nature - they depend on which source, category, and
    settlement date the candidate would add to - so they are answered here
    against the published budgets rather than recomputed from positions.
    """
    blocking = set(flags)

    if budgets.source_room(pair.settlement_source, limits.max_exposure_per_source) < (
        required_notional
    ):
        blocking.add(RiskFlag.SOURCE_CONCENTRATION)
    if budgets.category_room(pair.category, limits.max_exposure_per_category) < (
        required_notional
    ):
        blocking.add(RiskFlag.CATEGORY_CONCENTRATION)
    if budgets.settlement_date_room(
        pair.settlement_date, limits.max_exposure_per_settlement_date
    ) < required_notional:
        blocking.add(RiskFlag.SETTLEMENT_DATE_CONCENTRATION)
    if budgets.unsettled_capital_remaining < required_notional:
        blocking.add(RiskFlag.UNSETTLED_CAPITAL_CAP)

    return tuple(sorted(blocking, key=list(RiskFlag).index))


def _add(totals: dict[str, Decimal], key: str, amount: Decimal) -> None:
    totals[key] = totals.get(key, Decimal("0")) + amount


def _remaining(used: Mapping[str, Decimal], limit: Decimal) -> dict[str, Decimal]:
    return {key: limit - amount for key, amount in used.items()}
