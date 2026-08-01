"""Marginal-stop sizing - one fee-aware pass down both books.

The rule is that the walk stops at the contract whose *marginal* Net Edge turns
negative, rather than walking on gross edge and deducting fees from the total
afterward. Those two produce different sizes, and the difference is always a
contract the system knew would lose money and took anyway because an earlier
one paid for it.

The walk consumes both ask ladders in step. Levels do not line up across
venues, so each chunk is bounded by whichever side has less depth remaining at
its current level, and the two ladders advance independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arb.domain import Level
from arb.pricing import FeeSchedule, fee_breakdown, gross_edge

__all__ = ["Sizing", "walk"]


@dataclass(frozen=True, slots=True)
class Sizing:
    """The result of the walk: how many pairs to take, and at what limits."""

    size: int
    expected_profit: Decimal
    kalshi_notional: Decimal
    polymarket_notional: Decimal

    #: The worst price actually taken on each side - the limit that fills
    #: everything the walk counted on and nothing worse.
    kalshi_limit_price: Decimal | None = None
    polymarket_limit_price: Decimal | None = None

    @property
    def is_tradeable(self) -> bool:
        return self.size > 0


EMPTY = Sizing(
    size=0,
    expected_profit=Decimal("0"),
    kalshi_notional=Decimal("0"),
    polymarket_notional=Decimal("0"),
)


def walk(
    kalshi_asks: tuple[Level, ...],
    polymarket_asks: tuple[Level, ...],
    schedule: FeeSchedule,
) -> Sizing:
    """Accumulate contracts while Marginal Net Edge is positive; stop at zero.

    Both ladders must be ordered best price first.
    """
    kalshi_index = polymarket_index = 0
    kalshi_left = polymarket_left = 0
    size = 0
    profit = Decimal("0")
    kalshi_notional = Decimal("0")
    polymarket_notional = Decimal("0")
    kalshi_limit: Decimal | None = None
    polymarket_limit: Decimal | None = None

    while True:
        # Refill whichever side has exhausted its current level.
        if kalshi_left == 0:
            if kalshi_index >= len(kalshi_asks):
                break
            kalshi_left = kalshi_asks[kalshi_index].size
            kalshi_index += 1
        if polymarket_left == 0:
            if polymarket_index >= len(polymarket_asks):
                break
            polymarket_left = polymarket_asks[polymarket_index].size
            polymarket_index += 1

        kalshi_price = kalshi_asks[kalshi_index - 1].price
        polymarket_price = polymarket_asks[polymarket_index - 1].price

        # Marginal Net Edge is constant across a chunk, because a chunk is
        # exactly the run over which neither side's price changes.
        marginal = gross_edge(kalshi_price, polymarket_price) - fee_breakdown(
            kalshi_price, polymarket_price, schedule
        ).total
        if marginal <= 0:
            break

        chunk = min(kalshi_left, polymarket_left)
        size += chunk
        profit += marginal * chunk
        kalshi_notional += kalshi_price * chunk
        polymarket_notional += polymarket_price * chunk
        kalshi_limit = kalshi_price
        polymarket_limit = polymarket_price
        kalshi_left -= chunk
        polymarket_left -= chunk

    if size == 0:
        return EMPTY
    return Sizing(
        size=size,
        expected_profit=profit,
        kalshi_notional=kalshi_notional,
        polymarket_notional=polymarket_notional,
        kalshi_limit_price=kalshi_limit,
        polymarket_limit_price=polymarket_limit,
    )
