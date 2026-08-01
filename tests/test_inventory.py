"""Inventory-aware pair selection.

Both legs are paid on entry, but the payout lands entirely at whichever venue
holds the winning side. Per-venue balance therefore wanders, and rebalancing is
slow, costly, and the highest-scrutiny operation available - so the cheapest
place to manage Drift is in which pairs get taken.

What can actually be steered is narrower than it first looks, and the tests
below pin the honest version. See `arb.inventory` for why the lever is a
probability rather than an expectation.
"""

from __future__ import annotations

from decimal import Decimal

from dataclasses import replace

from arb.decisions import DecisionRecord
from arb.domain import Venue
from arb.inventory import rank_candidates, replenishment_probability
from arb.risk import RiskLimits
from tests.test_store import record


def candidate(
    pair_id: str, *, kalshi_price: str, polymarket_price: str, profit: str
) -> DecisionRecord:
    return replace(
        record(pair_id),
        kalshi_price=Decimal(kalshi_price),
        polymarket_price=Decimal(polymarket_price),
        expected_profit=Decimal(profit),
    )


def balances(kalshi: str, polymarket: str) -> dict[Venue, Decimal]:
    return {"kalshi": Decimal(kalshi), "polymarket": Decimal(polymarket)}


class TestReplenishmentProbability:
    def test_the_depleted_venue_is_replenished_when_its_own_leg_wins(self) -> None:
        """Buying the favourite on the depleted venue is what makes the payout
        likely to land there."""
        probability = replenishment_probability(
            kalshi_price=Decimal("0.90"),
            polymarket_price=Decimal("0.10"),
            venue_balances=balances(kalshi="100", polymarket="10000"),
        )

        assert probability == Decimal("0.90")

    def test_it_reads_the_other_leg_when_the_other_venue_is_depleted(self) -> None:
        probability = replenishment_probability(
            kalshi_price=Decimal("0.90"),
            polymarket_price=Decimal("0.10"),
            venue_balances=balances(kalshi="10000", polymarket="100"),
        )

        assert probability == Decimal("0.10")

    def test_balanced_venues_have_nothing_to_steer(self) -> None:
        probability = replenishment_probability(
            kalshi_price=Decimal("0.90"),
            polymarket_price=Decimal("0.10"),
            venue_balances=balances(kalshi="5000", polymarket="5000"),
        )

        assert probability == Decimal("0")


class TestRankingByProfit:
    def test_candidates_rank_by_total_net_profit_not_edge_percentage(self) -> None:
        """User story 30: a thin high-percentage opportunity must not outrank a
        fillable one. The 5%-edge candidate here is worth less in total."""
        thin_but_wide = candidate(
            "thin", kalshi_price="0.05", polymarket_price="0.90", profit="2"
        )
        narrow_but_deep = candidate(
            "deep", kalshi_price="0.40", polymarket_price="0.57", profit="50"
        )

        ranked = rank_candidates(
            [thin_but_wide, narrow_but_deep],
            venue_balances=balances("5000", "5000"),
            limits=RiskLimits(),
        )

        assert [c.pair_id for c in ranked] == ["deep", "thin"]

    def test_ranking_is_stable_for_equal_profit(self) -> None:
        """Ties broken by pair id, because the action trace has to replay
        identically and a tie is where ordering would otherwise drift."""
        ranked = rank_candidates(
            [
                candidate("zulu", kalshi_price="0.05", polymarket_price="0.90", profit="5"),
                candidate("alpha", kalshi_price="0.05", polymarket_price="0.90", profit="5"),
            ],
            venue_balances=balances("5000", "5000"),
            limits=RiskLimits(),
        )

        assert [c.pair_id for c in ranked] == ["alpha", "zulu"]


class TestSteering:
    def test_steering_is_off_until_a_venue_enters_the_steering_band(self) -> None:
        """No band configured means profit ranks alone - the spec proposes no
        default, so an unconfigured system does not silently sacrifice profit
        to an inventory goal nobody set."""
        replenishing = candidate(
            "replenishes", kalshi_price="0.90", polymarket_price="0.08", profit="10"
        )
        richer = candidate(
            "richer", kalshi_price="0.08", polymarket_price="0.90", profit="20"
        )

        ranked = rank_candidates(
            [replenishing, richer],
            venue_balances=balances(kalshi="100", polymarket="10000"),
            limits=RiskLimits(),
        )

        assert [c.pair_id for c in ranked] == ["richer", "replenishes"]

    def test_inside_the_band_the_replenishing_candidate_is_preferred(self) -> None:
        """The spec's coverage item: with balances skewed, the pair whose
        settlement refills the depleted venue wins even against more profit."""
        replenishing = candidate(
            "replenishes", kalshi_price="0.90", polymarket_price="0.08", profit="10"
        )
        richer = candidate(
            "richer", kalshi_price="0.08", polymarket_price="0.90", profit="20"
        )

        ranked = rank_candidates(
            [richer, replenishing],
            venue_balances=balances(kalshi="100", polymarket="10000"),
            limits=RiskLimits(steering_balance_band=Decimal("500")),
        )

        assert [c.pair_id for c in ranked] == ["replenishes", "richer"]

    def test_inside_the_band_profit_still_breaks_ties_between_equal_replenishers(
        self,
    ) -> None:
        ranked = rank_candidates(
            [
                candidate("small", kalshi_price="0.90", polymarket_price="0.08", profit="1"),
                candidate("large", kalshi_price="0.90", polymarket_price="0.08", profit="9"),
            ],
            venue_balances=balances(kalshi="100", polymarket="10000"),
            limits=RiskLimits(steering_balance_band=Decimal("500")),
        )

        assert [c.pair_id for c in ranked] == ["large", "small"]

    def test_a_healthy_book_is_not_steered_even_with_a_band_configured(self) -> None:
        ranked = rank_candidates(
            [
                candidate("replenishes", kalshi_price="0.90", polymarket_price="0.08", profit="10"),
                candidate("richer", kalshi_price="0.08", polymarket_price="0.90", profit="20"),
            ],
            venue_balances=balances(kalshi="9000", polymarket="10000"),
            limits=RiskLimits(steering_balance_band=Decimal("500")),
        )

        assert [c.pair_id for c in ranked] == ["richer", "replenishes"]
