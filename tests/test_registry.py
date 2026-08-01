"""The Pair Registry and the approval workflow.

Three stages, on their own clock, outside the reducer: the model **proposes**,
deterministic rules **verify**, the operator **approves** once. The model is a
generator with no authority - it cannot promote its own candidate past the rule
layer, and the operator never sees one the rules rejected.

The workflow also builds the calibration dataset: model confidence, rule
verdict, and operator decision recorded together. The spec is emphatic that
this cannot be retrofitted, so it exists from the first commit rather than
being added once someone wants the curve.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from arb.registry import (
    PairCandidate,
    PairStatus,
    approve,
    propose,
    registry_from,
    reject,
    revoke,
    verify_candidate,
)
from tests.test_verification import market, series


def proposed(confidence: str = "0.90") -> PairCandidate:
    return propose(
        pair_id="nfl-kc",
        kalshi=series(),
        polymarket=market(),
        category="sports",
        settlement_date="2026-09-10",
        model_confidence=Decimal(confidence),
        proposed_at_ms=1_000,
    )


def approved() -> PairCandidate:
    return approve(verify_candidate(proposed()), operator="max", at_ms=2_000)


class TestVerificationGatesTheOperator:
    def test_a_candidate_that_passes_the_rules_awaits_approval(self) -> None:
        candidate = verify_candidate(proposed())

        assert candidate.status is PairStatus.AWAITING_APPROVAL
        assert candidate.verdict is not None and candidate.verdict.verified

    def test_a_candidate_the_rules_reject_never_reaches_the_operator(self) -> None:
        candidate = verify_candidate(
            replace(proposed(), polymarket=market(void_rule="Void never"))
        )

        assert candidate.status is PairStatus.REJECTED_BY_RULES

    def test_the_operator_cannot_approve_a_rules_rejected_candidate(self) -> None:
        """The model proposes and the operator approves, but neither can
        overrule the deterministic layer."""
        candidate = verify_candidate(
            replace(proposed(), polymarket=market(void_rule="Void never"))
        )

        with pytest.raises(ValueError, match="rejected_by_rules"):
            approve(candidate, operator="max", at_ms=2_000)

    def test_an_unverified_candidate_cannot_be_approved(self) -> None:
        with pytest.raises(ValueError, match="proposed"):
            approve(proposed(), operator="max", at_ms=2_000)


class TestApproval:
    def test_approval_records_who_decided_and_when(self) -> None:
        candidate = approved()

        assert candidate.status is PairStatus.APPROVED
        assert candidate.operator == "max"
        assert candidate.decided_at_ms == 2_000

    def test_operator_rejection_is_recorded_with_its_reason(self) -> None:
        """A rejected pair is data too: it is the disagreement between model
        and operator that the calibration curve is built from."""
        candidate = reject(
            verify_candidate(proposed()),
            operator="max",
            at_ms=2_000,
            note="Kalshi settles on the league feed, Polymarket on a wire report",
        )

        assert candidate.status is PairStatus.REJECTED_BY_OPERATOR
        assert candidate.operator_note.startswith("Kalshi settles")

    def test_approving_twice_is_refused(self) -> None:
        with pytest.raises(ValueError, match="approved"):
            approve(approved(), operator="max", at_ms=3_000)


class TestRevocation:
    def test_an_approved_pair_can_be_revoked(self) -> None:
        """User story 17: a discovered flaw removes it from trading
        immediately."""
        candidate = revoke(approved(), at_ms=5_000, note="Overtime rule changed")

        assert candidate.status is PairStatus.REVOKED
        assert candidate.operator_note == "Overtime rule changed"

    def test_a_revoked_pair_leaves_the_registry(self) -> None:
        candidate = approved()
        assert registry_from([candidate]) != {}

        assert registry_from([revoke(candidate, at_ms=5_000, note="flaw")]) == {}


class TestTheRegistryTheReducerReads:
    def test_only_approved_pairs_become_tradeable(self) -> None:
        pending = verify_candidate(proposed())
        rules_rejected = verify_candidate(
            replace(proposed(), pair_id="bad", polymarket=market(void_rule="never"))
        )

        registry = registry_from([approved(), pending, rules_rejected])

        assert list(registry) == ["nfl-kc"]

    def test_the_matched_pair_carries_the_verified_settlement_source(self) -> None:
        """Exposure is capped per settlement source, so the reducer needs the
        source on the pair rather than having to look it back up."""
        matched = registry_from([approved()])["nfl-kc"]

        assert matched.settlement_source == "NFL official box score"
        assert matched.category == "sports"
        assert matched.settlement_date == "2026-09-10"

    def test_the_registry_is_ordered_so_the_reducer_is_deterministic(self) -> None:
        second = approve(
            verify_candidate(replace(proposed(), pair_id="aaa-first")),
            operator="max",
            at_ms=2_000,
        )

        assert list(registry_from([approved(), second])) == ["aaa-first", "nfl-kc"]


class TestTheCalibrationDataset:
    def test_confidence_verdict_and_decision_are_recorded_together(self) -> None:
        """User story 15: a later phase is justified by a calibration curve
        rather than a guess."""
        row = approved().as_record()

        assert row["pair_id"] == "nfl-kc"
        assert row["model_confidence"] == "0.90000000"
        assert row["rule_verified"] == "1"
        assert row["rule_failures"] == ""
        assert row["status"] == "approved"
        assert row["operator"] == "max"

    def test_a_rules_rejected_candidate_records_every_failure(self) -> None:
        row = verify_candidate(
            replace(
                proposed("0.55"),
                polymarket=market(void_rule="Void never", overtime_rule=None),
            )
        ).as_record()

        assert row["model_confidence"] == "0.55000000"
        assert row["rule_verified"] == "0"
        assert row["rule_failures"] == "unverifiable_overtime_rule,divergent_void_rule"
        assert row["status"] == "rejected_by_rules"

    def test_ground_truth_is_attachable_after_settlement(self) -> None:
        """User story 16: the calibration dataset needs labels, and the label
        only exists once both venues have paid."""
        labelled = replace(approved(), settled_identically=True)

        assert labelled.as_record()["settled_identically"] == "1"

    def test_ground_truth_is_blank_until_settlement(self) -> None:
        assert approved().as_record()["settled_identically"] == ""
