"""Marginal-stop sizing.

The spec's rule: a *single fee-aware pass* down both books, accumulating
contracts while **Marginal** Net Edge is positive and stopping at zero - not
walking on gross edge and deducting fees afterward.

The distinction has teeth. Averaging lets a profitable first level subsidise a
loss-making second one, and the system takes a contract it knows loses money.
Every expected value below is worked by hand from the spec's fee formula.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from arb.sizing import walk
from tests.builders import SPORTS_FEES, levels


def size_of(kalshi: list[tuple[str, int]], polymarket: list[tuple[str, int]]) -> int:
    return walk(levels(kalshi), levels(polymarket), SPORTS_FEES).size


class TestTheMarginalStop:
    def test_stops_when_the_next_contract_has_negative_gross_edge(self) -> None:
        # Level 2 is 1 - 0.08 - 0.93 = -0.01 gross: obviously not taken.
        assert size_of([("0.05", 100), ("0.08", 100)], [("0.90", 100), ("0.93", 100)]) == 100

    def test_refuses_a_contract_that_only_loses_after_fees(self) -> None:
        """The case that separates this from a gross-edge walk.

        Level 2 is 1 - 0.06 - 0.935 = +0.005 gross, so a gross walk takes it.
        Its fees are 0.07*0.06*0.94 + 0.05*0.935*0.065 = 0.00698675, so its
        marginal Net Edge is -0.00198675 and it must be refused.
        """
        assert size_of(
            [("0.05", 100), ("0.06", 100)], [("0.90", 100), ("0.935", 100)]
        ) == 100

    def test_does_not_let_a_profitable_level_subsidise_a_losing_one(self) -> None:
        """Averaged over both levels the pair still looks profitable, which is
        exactly the trap. Size must not grow past the marginal stop."""
        sized = walk(
            levels([("0.05", 100), ("0.06", 100)]),
            levels([("0.90", 100), ("0.935", 100)]),
            SPORTS_FEES,
        )
        assert sized.size == 100
        assert sized.expected_profit == Decimal("4.2175")

    def test_a_book_with_no_profitable_level_sizes_to_zero(self) -> None:
        assert size_of([("0.50", 100)], [("0.49", 100)]) == 0

    def test_an_empty_book_sizes_to_zero(self) -> None:
        assert size_of([], [("0.90", 100)]) == 0
        assert size_of([("0.05", 100)], []) == 0


class TestDepth:
    def test_size_is_capped_by_the_thinner_book(self) -> None:
        """User story 28: never commit to a size only one venue can fill."""
        assert size_of([("0.05", 100)], [("0.90", 40)]) == 40
        assert size_of([("0.05", 40)], [("0.90", 100)]) == 40

    def test_the_walk_advances_each_book_independently(self) -> None:
        """Levels do not line up across venues. Consuming 30 contracts on one
        venue must leave the other venue's level partly intact.

        Kalshi 0.05x30 then 0.055x200; Polymarket 0.90x100. The walk takes 30
        against the first Kalshi level and 70 against the second, stopping when
        Polymarket runs out.
        """
        assert size_of([("0.05", 30), ("0.055", 200)], [("0.90", 100)]) == 100

    def test_expected_profit_sums_the_marginal_edges_not_the_top_of_book_edge(
        self,
    ) -> None:
        """30 contracts at net 0.042175, then 70 at net 0.03686175."""
        sized = walk(
            levels([("0.05", 30), ("0.055", 200)]),
            levels([("0.90", 100)]),
            SPORTS_FEES,
        )
        assert sized.size == 100
        assert sized.expected_profit == pytest.approx(
            Decimal("3.8455725"), abs=Decimal("0.0000001")
        )


class TestOrderPrices:
    def test_the_limit_price_is_the_worst_level_actually_taken(self) -> None:
        """Legging needs a limit that fills everything the walk counted on and
        nothing worse than that."""
        sized = walk(
            levels([("0.05", 30), ("0.055", 200)]),
            levels([("0.90", 100)]),
            SPORTS_FEES,
        )
        assert sized.kalshi_limit_price == Decimal("0.055")
        assert sized.polymarket_limit_price == Decimal("0.90")

    def test_a_zero_size_walk_has_no_limit_prices(self) -> None:
        sized = walk(levels([("0.50", 100)]), levels([("0.49", 100)]), SPORTS_FEES)

        assert sized.size == 0
        assert sized.kalshi_limit_price is None
        assert sized.polymarket_limit_price is None

    def test_notionals_are_what_each_venue_actually_costs(self) -> None:
        """Both legs are paid on entry, and they are paid in different places -
        which is where capital Drift comes from."""
        sized = walk(
            levels([("0.05", 30), ("0.055", 200)]),
            levels([("0.90", 100)]),
            SPORTS_FEES,
        )
        # 30*0.05 + 70*0.055 = 1.5 + 3.85
        assert sized.kalshi_notional == Decimal("5.35")
        assert sized.polymarket_notional == Decimal("90.00")
