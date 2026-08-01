"""The verdict report.

The deliverable. What it has to support is a claim of the form "over N weeks,
X% of evaluated candidates were positive after fees, concentrated in the tails,
and Y% of that survived execution" - a base rate rather than a pile of
anecdotes.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from arb.decisions import DecisionRecord, RejectionReason
from arb.report import build_report
from arb.settlement import SettlementRecord
from tests.test_store import record


def decision(
    pair_id: str,
    *,
    kalshi: str,
    polymarket: str,
    net: str,
    accepted: bool = True,
    reason: RejectionReason | None = None,
    profit: str = "0",
) -> DecisionRecord:
    return replace(
        record(pair_id, accepted=accepted, reason=reason),
        kalshi_price=Decimal(kalshi),
        polymarket_price=Decimal(polymarket),
        net_edge=Decimal(net),
        expected_profit=Decimal(profit),
    )


def settlement(
    pair_id: str, *, predicted: str, realised: str, mismatch: bool = False
) -> SettlementRecord:
    return SettlementRecord(
        pair_id=pair_id,
        size=100,
        kalshi_payout=Decimal("1"),
        polymarket_payout=Decimal("0") if not mismatch else Decimal("1"),
        kalshi_settled_at_ms=9_000,
        polymarket_settled_at_ms=9_000,
        cost=Decimal("95"),
        fees_paid=Decimal("0.78"),
        realised_profit=Decimal(realised),
        predicted_profit=Decimal(predicted),
    )


class TestTheFunnel:
    def test_every_evaluation_counts_toward_the_denominator(self) -> None:
        report = build_report(
            [
                decision("a", kalshi="0.05", polymarket="0.90", net="0.04"),
                decision(
                    "b",
                    kalshi="0.50",
                    polymarket="0.49",
                    net="-0.02",
                    accepted=False,
                    reason=RejectionReason.NEGATIVE_NET_EDGE,
                ),
                decision(
                    "c",
                    kalshi="0.50",
                    polymarket="0.49",
                    net="-0.02",
                    accepted=False,
                    reason=RejectionReason.NEGATIVE_NET_EDGE,
                ),
            ]
        )

        assert report.funnel.evaluated == 3
        assert report.funnel.positive_after_fees == 1
        assert report.funnel.accepted == 1

    def test_the_base_rate_is_positives_over_everything_evaluated(self) -> None:
        report = build_report(
            [
                decision("a", kalshi="0.05", polymarket="0.90", net="0.04"),
                decision(
                    "b",
                    kalshi="0.50",
                    polymarket="0.49",
                    net="-0.02",
                    accepted=False,
                    reason=RejectionReason.NEGATIVE_NET_EDGE,
                ),
                decision(
                    "c",
                    kalshi="0.50",
                    polymarket="0.49",
                    net="-0.02",
                    accepted=False,
                    reason=RejectionReason.NEGATIVE_NET_EDGE,
                ),
                decision(
                    "d",
                    kalshi="0.50",
                    polymarket="0.49",
                    net="-0.02",
                    accepted=False,
                    reason=RejectionReason.NEGATIVE_NET_EDGE,
                ),
            ]
        )

        assert report.funnel.base_rate == Decimal("0.25")

    def test_rejections_are_broken_out_by_reason(self) -> None:
        """"No opportunity existed" has to be separable from "the system
        filtered it out"."""
        report = build_report(
            [
                decision(
                    "a",
                    kalshi="0.05",
                    polymarket="0.90",
                    net="0.04",
                    accepted=False,
                    reason=RejectionReason.EXCESSIVE_SKEW,
                ),
                decision(
                    "b",
                    kalshi="0.50",
                    polymarket="0.49",
                    net="-0.02",
                    accepted=False,
                    reason=RejectionReason.NEGATIVE_NET_EDGE,
                ),
            ]
        )

        assert report.funnel.rejections == {
            "excessive_skew": 1,
            "negative_net_edge": 1,
        }

    def test_a_gate_that_removed_a_real_edge_is_visible(self) -> None:
        """A skew-rejected candidate was still positive after fees, so it
        counts toward the base rate but not toward accepted. The gap between
        those two numbers is what the gates cost."""
        report = build_report(
            [
                decision(
                    "a",
                    kalshi="0.05",
                    polymarket="0.90",
                    net="0.04",
                    accepted=False,
                    reason=RejectionReason.EXCESSIVE_SKEW,
                )
            ]
        )

        assert report.funnel.positive_after_fees == 1
        assert report.funnel.accepted == 0
        assert report.funnel.fill_rate == Decimal("0")

    def test_an_empty_log_does_not_divide_by_zero(self) -> None:
        report = build_report([])

        assert report.funnel.evaluated == 0
        assert report.funnel.base_rate == Decimal("0")
        assert report.execution.realisation_ratio == Decimal("0")


class TestTheTailsPrediction:
    def test_candidates_are_bucketed_by_the_cheaper_leg(self) -> None:
        """A matched pair has p1 + p2 ~= 1, so the cheaper leg locates it on
        the fee parabola."""
        report = build_report(
            [
                decision("tail", kalshi="0.03", polymarket="0.95", net="0.02"),
                decision("mid", kalshi="0.20", polymarket="0.78", net="0.01"),
                decision(
                    "money",
                    kalshi="0.49",
                    polymarket="0.50",
                    net="-0.02",
                    accepted=False,
                    reason=RejectionReason.NEGATIVE_NET_EDGE,
                ),
            ]
        )
        counts = {band.band: band.evaluated for band in report.bands}

        assert counts == {
            "0.00-0.05": 1,
            "0.05-0.10": 0,
            "0.10-0.25": 1,
            "0.25-0.50": 1,
        }

    def test_the_bucketing_can_actually_refute_the_prediction(self) -> None:
        """If the edge turned out to live at the money, the report would say
        so - the buckets are not rigged to agree."""
        report = build_report(
            [
                decision("money", kalshi="0.49", polymarket="0.49", net="0.01"),
                decision(
                    "tail",
                    kalshi="0.03",
                    polymarket="0.98",
                    net="-0.01",
                    accepted=False,
                    reason=RejectionReason.NEGATIVE_NET_EDGE,
                ),
            ]
        )
        by_band = {band.band: band for band in report.bands}

        assert by_band["0.25-0.50"].base_rate == Decimal("1")
        assert by_band["0.00-0.05"].base_rate == Decimal("0")

    def test_it_reads_the_cheaper_leg_whichever_venue_it_is_on(self) -> None:
        report = build_report(
            [decision("t", kalshi="0.95", polymarket="0.03", net="0.02")]
        )
        counts = {band.band: band.evaluated for band in report.bands}

        assert counts["0.00-0.05"] == 1

    def test_expected_profit_accumulates_per_band(self) -> None:
        report = build_report(
            [
                decision("a", kalshi="0.03", polymarket="0.95", net="0.02", profit="5"),
                decision("b", kalshi="0.04", polymarket="0.94", net="0.02", profit="7"),
            ]
        )
        by_band = {band.band: band for band in report.bands}

        assert by_band["0.00-0.05"].expected_profit == Decimal("12")


class TestExecution:
    def test_predicted_is_compared_against_realised(self) -> None:
        """User story 7: how much of the paper edge survives execution."""
        report = build_report(
            [],
            [
                settlement("a", predicted="4.00", realised="3.00"),
                settlement("b", predicted="4.00", realised="3.00"),
            ],
        )

        assert report.execution.predicted_profit == Decimal("8.00")
        assert report.execution.realised_profit == Decimal("6.00")
        assert report.execution.realisation_ratio == Decimal("0.75")

    def test_settlement_mismatches_are_surfaced_not_netted_away(self) -> None:
        """A mismatch that happened to be profitable must not disappear into a
        healthy-looking P&L line."""
        report = build_report(
            [],
            [
                settlement("a", predicted="4.00", realised="4.00"),
                settlement("b", predicted="4.00", realised="99.00", mismatch=True),
            ],
        )

        assert report.execution.mismatches == 1
        assert report.execution.mismatch_rate == Decimal("0.5")


class TestRendering:
    def test_the_report_renders_the_three_questions(self) -> None:
        report = build_report(
            [decision("a", kalshi="0.05", polymarket="0.90", net="0.04", profit="4")],
            [settlement("a", predicted="4.00", realised="3.00")],
        )
        rendered = report.render()

        assert "candidates evaluated      1" in rendered
        assert "0.05-0.10" in rendered
        assert "realisation ratio         0.75000000" in rendered

    def test_the_report_states_no_verdict(self) -> None:
        """The spec left the criteria open on purpose. Printing a PASS here
        would invent the standard the project is meant to be judged against."""
        rendered = build_report([]).render()

        assert "PASS" not in rendered.upper().replace("PASSED", "")
        assert "VERDICT:" not in rendered.upper()
