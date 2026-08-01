"""Fee model and Net Edge.

Expected values here come from the spec, not from re-running the
implementation's own arithmetic.

The spec's `Fee model` section gives the Fee Hurdle in closed form:

    fee_per_contract = (0.07 + theta_category) * p * (1 - p)

    "For sports and economics (theta = 0.05) this is 0.12 * p(1-p) - about
     3.00 cents at the money, 1.08 cents at p=0.10, 0.57 cents at p=0.05."

That single-price form is an approximation justified by p1 + p2 ~= 1. User
story 25 asks for the honest version - "real per-contract fees on both legs at
their actual prices" - so `net_edge` charges each leg its own venue rate at its
own price, and the expected values below are worked by hand from that.
"""

from decimal import Decimal

import pytest

from arb.pricing import FeeSchedule, fee_breakdown, fee_per_contract, gross_edge, net_edge

SPORTS = FeeSchedule(kalshi_rate=Decimal("0.07"), polymarket_rate=Decimal("0.05"))


class TestFeeHurdle:
    """The spec's closed form, at a single price."""

    @pytest.mark.parametrize(
        ("price", "expected_cents"),
        [
            (Decimal("0.50"), Decimal("3.00")),
            (Decimal("0.10"), Decimal("1.08")),
            (Decimal("0.05"), Decimal("0.57")),
        ],
    )
    def test_matches_the_spec_worked_examples(
        self, price: Decimal, expected_cents: Decimal
    ) -> None:
        assert fee_per_contract(price, SPORTS) * 100 == pytest.approx(
            expected_cents, abs=Decimal("0.005")
        )

    def test_is_a_parabola_peaking_at_the_money(self) -> None:
        """The spec's central claim: fees peak at 0.50, so the strategy is
        structurally viable only in the tails."""
        at_the_money = fee_per_contract(Decimal("0.50"), SPORTS)
        for price in ("0.05", "0.10", "0.25", "0.75", "0.90", "0.95"):
            assert fee_per_contract(Decimal(price), SPORTS) < at_the_money

    def test_is_symmetric_about_the_money(self) -> None:
        assert fee_per_contract(Decimal("0.10"), SPORTS) == fee_per_contract(
            Decimal("0.90"), SPORTS
        )

    def test_category_rate_is_configuration_not_a_constant(self) -> None:
        """Both venues have changed fees recently, so theta is per-category."""
        cheap = FeeSchedule(kalshi_rate=Decimal("0.07"), polymarket_rate=Decimal("0"))
        assert fee_per_contract(Decimal("0.50"), cheap) < fee_per_contract(
            Decimal("0.50"), SPORTS
        )


class TestFeeBreakdown:
    def test_charges_each_leg_its_own_venue_rate_at_its_own_price(self) -> None:
        # By hand: Kalshi 0.07 * 0.10 * 0.90 = 0.0063
        #          Polymarket 0.05 * 0.88 * 0.12 = 0.00528
        breakdown = fee_breakdown(Decimal("0.10"), Decimal("0.88"), SPORTS)
        assert breakdown.kalshi_fee == Decimal("0.0063")
        assert breakdown.polymarket_fee == Decimal("0.00528")
        assert breakdown.total == Decimal("0.01158")

    def test_is_storable_for_reanalysis_under_a_different_fee_schedule(self) -> None:
        """User story 3: the breakdown is persisted per candidate so the
        analysis can be re-run without recollecting data."""
        breakdown = fee_breakdown(Decimal("0.10"), Decimal("0.88"), SPORTS)
        assert breakdown.as_record() == {
            "kalshi_rate": "0.07000000",
            "polymarket_rate": "0.05000000",
            "kalshi_price": "0.10000000",
            "polymarket_price": "0.88000000",
            "kalshi_fee": "0.00630000",
            "polymarket_fee": "0.00528000",
            "total": "0.01158000",
        }

    def test_serialisation_does_not_depend_on_how_the_inputs_were_written(self) -> None:
        """Byte-identical replay requires that equal values render equally."""
        terse = fee_breakdown(Decimal("0.1"), Decimal("0.88"), SPORTS)
        padded = fee_breakdown(Decimal("0.1000"), Decimal("0.88"), SPORTS)
        assert terse.as_record() == padded.as_record()


class TestNetEdge:
    def test_gross_edge_is_one_minus_both_prices(self) -> None:
        assert gross_edge(Decimal("0.40"), Decimal("0.55")) == Decimal("0.05")

    def test_net_edge_is_gross_edge_minus_the_fee_breakdown(self) -> None:
        # By hand: gross = 1 - 0.10 - 0.88 = 0.02; fees = 0.01158 (above).
        assert net_edge(Decimal("0.10"), Decimal("0.88"), SPORTS) == Decimal("0.00842")

    def test_net_edge_can_be_negative(self) -> None:
        """The structural defect this replaces: a proportional haircut on a
        non-negative quantity can never mark an opportunity unprofitable."""
        # 1c gross at the money, against a ~3c fee hurdle.
        assert net_edge(Decimal("0.50"), Decimal("0.49"), SPORTS) < 0

    def test_a_thin_edge_at_the_money_loses_but_the_same_edge_in_the_tail_wins(
        self,
    ) -> None:
        """Same 2c gross edge; only the tail survives the fee parabola."""
        assert net_edge(Decimal("0.50"), Decimal("0.48"), SPORTS) < 0
        assert net_edge(Decimal("0.05"), Decimal("0.93"), SPORTS) > 0
