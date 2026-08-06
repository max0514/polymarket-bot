"""Seam 2 - pair verification.

`verify(kalshi_series, polymarket_market) -> Verdict`. Pure, and tested
separately from the reducer because it gates real money on a slower clock.

The governing rule from the spec: this layer gates on *machine-checkable
identity*, not inference. Anything not mechanically verifiable is rejected, and
any divergence rejects outright rather than being weighed for severity under
time pressure.
"""

from __future__ import annotations

from dataclasses import replace

from arb.verification import (
    ContractTerms,
    Failure,
    KalshiSeries,
    PolymarketMarket,
    verify,
)

MATCHING_TERMS = ContractTerms(
    settlement_source="NFL official box score",
    settling_release="Final box score",
    settling_release_timestamp=None,
    revisable=False,
    void_rule="Void if the game is not played by 2026-09-15",
    postponement_rule="Postponement beyond 48 hours voids the market",
    overtime_rule="Overtime counts toward the final score",
    threshold="Team with the higher final score",
    tie_break_rule="A tie resolves No",
)


def series(**overrides: object) -> KalshiSeries:
    return KalshiSeries(
        series_ticker="KXNFLGAME",
        title="Will Kansas City win?",
        contract_terms_url="https://kalshi.com/terms/KXNFLGAME",
        terms=replace(MATCHING_TERMS, **overrides),  # type: ignore[arg-type]
    )


def market(**overrides: object) -> PolymarketMarket:
    return PolymarketMarket(
        condition_id="0xabc",
        question="Will Kansas City win?",
        resolution_source_url="https://polymarket.com/rules/0xabc",
        terms=replace(MATCHING_TERMS, **overrides),  # type: ignore[arg-type]
    )


class TestIdenticalTerms:
    def test_identical_terms_verify(self) -> None:
        verdict = verify(series(), market())

        assert verdict.verified is True
        assert verdict.failures == ()

    def test_formatting_differences_alone_do_not_reject(self) -> None:
        """Case and whitespace are presentation, not terms. Rejecting on them
        would make the gate reject everything and teach the operator to
        override it."""
        verdict = verify(
            series(settlement_source="NFL official box score"),
            market(settlement_source="  NFL Official Box Score  "),
        )

        assert verdict.verified is True


class TestDivergentTerms:
    def test_divergent_settlement_sources_reject(self) -> None:
        verdict = verify(
            series(settlement_source="CF Benchmarks BRTI"),
            market(settlement_source="Chainlink"),
        )

        assert verdict.verified is False
        assert Failure.DIVERGENT_SETTLEMENT_SOURCE in verdict.failures

    def test_divergent_void_rules_reject(self) -> None:
        verdict = verify(
            series(), market(void_rule="Void if the game is not played by 2026-10-01")
        )

        assert verdict.verified is False
        assert Failure.DIVERGENT_VOID_RULE in verdict.failures

    def test_divergent_postponement_rules_reject(self) -> None:
        verdict = verify(
            series(),
            market(postponement_rule="Postponement resolves the market No"),
        )

        assert verdict.verified is False
        assert Failure.DIVERGENT_POSTPONEMENT_RULE in verdict.failures

    def test_divergent_overtime_rules_reject(self) -> None:
        verdict = verify(
            series(), market(overtime_rule="Overtime is excluded from the final score")
        )

        assert verdict.verified is False
        assert Failure.DIVERGENT_OVERTIME_RULE in verdict.failures

    def test_divergent_thresholds_reject(self) -> None:
        verdict = verify(series(), market(threshold="Team leading at full time"))

        assert verdict.verified is False
        assert Failure.DIVERGENT_THRESHOLD in verdict.failures

    def test_divergent_tie_break_rules_reject(self) -> None:
        verdict = verify(series(), market(tie_break_rule="A tie resolves Yes"))

        assert verdict.verified is False
        assert Failure.DIVERGENT_TIE_BREAK_RULE in verdict.failures

    def test_every_divergence_is_reported_not_just_the_first(self) -> None:
        """The reviewer gets the whole diff. Reporting one failure at a time
        turns one rejection into several review cycles."""
        verdict = verify(
            series(),
            market(
                void_rule="Void never",
                overtime_rule="Overtime is excluded from the final score",
                tie_break_rule="A tie resolves Yes",
            ),
        )

        assert set(verdict.failures) == {
            Failure.DIVERGENT_VOID_RULE,
            Failure.DIVERGENT_OVERTIME_RULE,
            Failure.DIVERGENT_TIE_BREAK_RULE,
        }


class TestUnverifiableTerms:
    def test_a_field_missing_on_one_side_rejects_rather_than_passes(self) -> None:
        """Fails closed: silence is not agreement."""
        verdict = verify(series(), market(void_rule=None))

        assert verdict.verified is False
        assert Failure.UNVERIFIABLE_VOID_RULE in verdict.failures

    def test_a_core_field_missing_on_both_sides_still_rejects(self) -> None:
        """Two silences on a core field are not a match - nothing was checked,
        and a pair with no stated settlement source must never trade."""
        verdict = verify(
            series(settlement_source=None), market(settlement_source=None)
        )

        assert verdict.verified is False
        assert Failure.UNVERIFIABLE_SETTLEMENT_SOURCE in verdict.failures

    def test_mutual_silence_on_an_inapplicability_field_passes(self) -> None:
        """Baseball cannot tie and extra innings are part of the result, so
        neither venue states those rules. Mutual silence there is agreement by
        inapplicability - one-sided silence still rejects (see below)."""
        verdict = verify(
            series(overtime_rule=None, tie_break_rule=None),
            market(overtime_rule=None, tie_break_rule=None),
        )

        assert verdict.verified is True

    def test_one_sided_silence_on_an_inapplicability_field_still_rejects(
        self,
    ) -> None:
        verdict = verify(series(), market(tie_break_rule=None))

        assert verdict.verified is False
        assert Failure.UNVERIFIABLE_TIE_BREAK_RULE in verdict.failures

    def test_an_empty_string_counts_as_unstated(self) -> None:
        verdict = verify(series(), market(settlement_source="   "))

        assert verdict.verified is False
        assert Failure.UNVERIFIABLE_SETTLEMENT_SOURCE in verdict.failures


class TestRevisableReleases:
    def test_a_revisable_source_without_a_pinned_timestamp_rejects(self) -> None:
        """User story 18: a later revision must not be able to split a pair
        that agreed at publication."""
        verdict = verify(
            series(revisable=True, settling_release_timestamp=None),
            market(revisable=True, settling_release_timestamp=None),
        )

        assert verdict.verified is False
        assert Failure.RELEASE_NOT_PINNED in verdict.failures

    def test_a_revisable_source_pinned_identically_on_both_sides_verifies(self) -> None:
        verdict = verify(
            series(revisable=True, settling_release_timestamp="2026-09-04T12:30:00Z"),
            market(revisable=True, settling_release_timestamp="2026-09-04T12:30:00Z"),
        )

        assert verdict.verified is True

    def test_pinned_timestamps_that_disagree_reject(self) -> None:
        verdict = verify(
            series(revisable=True, settling_release_timestamp="2026-09-04T12:30:00Z"),
            market(revisable=True, settling_release_timestamp="2026-09-04T14:00:00Z"),
        )

        assert verdict.verified is False
        assert Failure.DIVERGENT_RELEASE_TIMESTAMP in verdict.failures

    def test_a_source_revisable_on_only_one_side_rejects(self) -> None:
        """Disagreement about whether the source can be revised is itself a
        rule divergence."""
        verdict = verify(
            series(revisable=True, settling_release_timestamp="2026-09-04T12:30:00Z"),
            market(revisable=False, settling_release_timestamp="2026-09-04T12:30:00Z"),
        )

        assert verdict.verified is False
        assert Failure.DIVERGENT_REVISABILITY in verdict.failures

    def test_a_non_revisable_source_needs_no_pinned_timestamp(self) -> None:
        verdict = verify(
            series(revisable=False, settling_release_timestamp=None),
            market(revisable=False, settling_release_timestamp=None),
        )

        assert verdict.verified is True


class TestVerdictRecord:
    def test_the_verdict_is_serialisable_for_the_calibration_dataset(self) -> None:
        verdict = verify(series(), market(void_rule=None))

        assert verdict.as_record() == {
            "verified": "0",
            "failures": "unverifiable_void_rule",
        }

    def test_failures_serialise_in_a_stable_order(self) -> None:
        """The calibration dataset is compared across runs, so the same verdict
        must always render the same way."""
        first = verify(
            series(), market(tie_break_rule="A tie resolves Yes", void_rule="Void never")
        )
        second = verify(
            series(), market(void_rule="Void never", tie_break_rule="A tie resolves Yes")
        )

        assert first.as_record() == second.as_record()
