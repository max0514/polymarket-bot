"""Fee model, Gross Edge, and Net Edge.

This replaces the proportional haircut in `scripts/arbitrage_dashboard.py`,
which multiplied a non-negative profit by `(1 - haircut)` and therefore could
never mark a candidate unprofitable. Net Edge here is a subtraction, so it goes
negative exactly when the real cost of trading exceeds the price gap.

Both venues charge roughly `rate * p * (1 - p)` per contract. That is a
parabola peaking at p=0.50, which is why the strategy is structurally viable
only in the tails.

All arithmetic is `Decimal`. Every input is a finite decimal and Decimal
multiplication of finite decimals is exact, so results are reproducible
bit-for-bit across runs - a precondition for the replay harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arb.canonical import canonical_decimal

__all__ = [
    "FeeBreakdown",
    "FeeSchedule",
    "fee_breakdown",
    "fee_per_contract",
    "gross_edge",
    "net_edge",
]

ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Per-contract fee rates for the two venues.

    Rates are configuration, not constants: both venues have changed them
    recently, and Polymarket's is category-dependent (`theta_category` in the
    spec), so it is resolved per market rather than averaged into one wrong
    number.
    """

    kalshi_rate: Decimal
    polymarket_rate: Decimal

    @property
    def combined_rate(self) -> Decimal:
        return self.kalshi_rate + self.polymarket_rate


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    """Per-leg fee detail, persisted alongside every Decision Record.

    Storing the rates and prices as well as the resulting fees is what lets the
    analysis be re-run under a different fee schedule without recollecting data
    (user story 3).
    """

    schedule: FeeSchedule
    kalshi_price: Decimal
    polymarket_price: Decimal
    kalshi_fee: Decimal
    polymarket_fee: Decimal

    @property
    def total(self) -> Decimal:
        return self.kalshi_fee + self.polymarket_fee

    def as_record(self) -> dict[str, str]:
        return {
            "kalshi_rate": canonical_decimal(self.schedule.kalshi_rate),
            "polymarket_rate": canonical_decimal(self.schedule.polymarket_rate),
            "kalshi_price": canonical_decimal(self.kalshi_price),
            "polymarket_price": canonical_decimal(self.polymarket_price),
            "kalshi_fee": canonical_decimal(self.kalshi_fee),
            "polymarket_fee": canonical_decimal(self.polymarket_fee),
            "total": canonical_decimal(self.total),
        }


def fee_per_contract(price: Decimal, schedule: FeeSchedule) -> Decimal:
    """The Fee Hurdle in the spec's closed form: combined cost at a single price.

    Valid because a matched pair has `p1 + p2 ~= 1` and `p(1-p)` is symmetric
    about 0.50, so both legs sit at nearly the same `p(1-p)`. Used for the
    tails analysis and for reporting; `net_edge` uses the exact per-leg form.
    """
    return schedule.combined_rate * price * (ONE - price)


def fee_breakdown(
    kalshi_price: Decimal, polymarket_price: Decimal, schedule: FeeSchedule
) -> FeeBreakdown:
    """Charge each leg its own venue's rate at its own price."""
    return FeeBreakdown(
        schedule=schedule,
        kalshi_price=kalshi_price,
        polymarket_price=polymarket_price,
        kalshi_fee=schedule.kalshi_rate * kalshi_price * (ONE - kalshi_price),
        polymarket_fee=(
            schedule.polymarket_rate * polymarket_price * (ONE - polymarket_price)
        ),
    )


def gross_edge(kalshi_price: Decimal, polymarket_price: Decimal) -> Decimal:
    """`1 - p1 - p2` for a pair bought on both sides, before fees."""
    return ONE - kalshi_price - polymarket_price


def net_edge(
    kalshi_price: Decimal, polymarket_price: Decimal, schedule: FeeSchedule
) -> Decimal:
    """Gross Edge minus the real per-leg fees. Negative when unprofitable."""
    return (
        gross_edge(kalshi_price, polymarket_price)
        - fee_breakdown(kalshi_price, polymarket_price, schedule).total
    )
