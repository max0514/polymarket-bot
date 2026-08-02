"""The pair review view - what the operator actually looks at.

User story 12 asks for both venues' contract terms fetched and diffed on void
policy, postponement handling, overtime treatment, and settling release
timestamp. This is that diff.

The governing property: the review and the gate must never disagree. The view
is built from the same `verify()` call that decides whether a pair is tradeable,
so an operator cannot be shown a green row for a term the rule layer rejected.
A review screen that draws its own conclusions is worse than no review screen.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from arb.registry import PairStatus, approve, verify_candidate
from arb.review import PairReview, TermRow, TermStatus, review_pair
from arb.verification import Failure
from tests.test_registry import proposed
from tests.test_verification import market


def review(**polymarket_overrides: object) -> PairReview:
    candidate = proposed()
    if polymarket_overrides:
        candidate = replace(candidate, polymarket=market(**polymarket_overrides))
    return review_pair(candidate)


def row(view: PairReview, field: str) -> TermRow:
    return next(r for r in view.rows if r.field == field)


class TestTheDiff:
    def test_matching_terms_all_agree(self) -> None:
        view = review()

        assert all(r.status is TermStatus.AGREES for r in view.rows)
        assert view.verified is True

    def test_the_four_terms_the_spec_names_are_all_shown(self) -> None:
        """Void policy, postponement, overtime, settling release timestamp."""
        fields = {r.field for r in review().rows}

        assert {
            "void_rule",
            "postponement_rule",
            "overtime_rule",
            "settling_release_timestamp",
        } <= fields

    def test_a_divergent_term_is_marked_and_shows_both_sides(self) -> None:
        view = review(void_rule="Void if not played by 2026-10-01")
        void = row(view, "void_rule")

        assert void.status is TermStatus.DIFFERS
        assert void.kalshi == "Void if the game is not played by 2026-09-15"
        assert void.polymarket == "Void if not played by 2026-10-01"

    def test_an_unstated_term_is_marked_separately_from_a_divergent_one(self) -> None:
        """"They disagree" and "nobody said" need different responses, so they
        are never collapsed into one colour."""
        view = review(overtime_rule=None)

        assert row(view, "overtime_rule").status is TermStatus.UNSTATED

    def test_terms_that_differ_only_in_formatting_still_agree(self) -> None:
        view = review(settlement_source="  NFL Official Box Score  ")

        assert row(view, "settlement_source").status is TermStatus.AGREES

    def test_every_row_carries_a_human_label(self) -> None:
        """The operator is reading contract terms under time pressure, not
        field names."""
        assert row(review(), "void_rule").label == "Void policy"
        assert row(review(), "settling_release_timestamp").label == "Release pinned to"


class TestTheReviewAgreesWithTheGate:
    def test_a_rejected_pair_reviews_as_unverified(self) -> None:
        view = review(void_rule="Void never")

        assert view.verified is False
        assert Failure.DIVERGENT_VOID_RULE in view.failures

    def test_the_rows_flagged_are_exactly_the_terms_the_gate_rejected(self) -> None:
        """No row may look fine while the gate is refusing it."""
        view = review(void_rule="Void never", overtime_rule=None)

        flagged = {r.field for r in view.rows if r.status is not TermStatus.AGREES}
        assert flagged == {"void_rule", "overtime_rule"}

    def test_a_pair_the_rules_reject_cannot_be_approved_from_the_view(self) -> None:
        """The screen offers no override. Severity judgements made under time
        pressure are exactly what the deterministic layer exists to prevent."""
        view = review(void_rule="Void never")

        assert view.can_approve is False

    def test_a_verified_pair_is_approvable(self) -> None:
        assert review().can_approve is True


class TestReleasePinning:
    def test_a_revisable_source_without_a_pinned_release_is_flagged(self) -> None:
        candidate = proposed()
        candidate = replace(
            candidate,
            kalshi=replace(
                candidate.kalshi,
                terms=replace(candidate.kalshi.terms, revisable=True),
            ),
            polymarket=market(revisable=True),
        )

        view = review_pair(candidate)

        assert view.verified is False
        assert Failure.RELEASE_NOT_PINNED in view.failures

    def test_a_non_revisable_source_says_so_rather_than_showing_blank(self) -> None:
        """A blank cell reads as missing data. This one is a real answer."""
        assert row(review(), "settling_release_timestamp").kalshi == (
            "not revisable - no pin needed"
        )


class TestWhatTheOperatorSees:
    def test_the_model_confidence_is_shown_before_the_decision(self) -> None:
        """Recorded alongside the operator's own decision, so a later
        calibration curve has both halves."""
        assert review().model_confidence == "0.90"

    def test_the_contract_sources_are_linked_for_checking(self) -> None:
        view = review()

        assert view.kalshi_url == "https://kalshi.com/terms/KXNFLGAME"
        assert view.polymarket_url == "https://polymarket.com/rules/0xabc"

    def test_an_already_decided_pair_is_not_offered_again(self) -> None:
        """User story 14: approve once, then it trades automatically."""
        decided = approve(verify_candidate(proposed()), operator="max", at_ms=2_000)

        view = review_pair(decided)

        assert view.can_approve is False
        assert view.status is PairStatus.APPROVED


class TestQueueOrdering:
    def test_pairs_awaiting_a_decision_come_first(self) -> None:
        """The queue is a work list. Anything already decided is reference."""
        from arb.review import review_queue

        pending = verify_candidate(proposed())
        decided = approve(
            verify_candidate(replace(proposed(), pair_id="zzz-decided")),
            operator="max",
            at_ms=2_000,
        )

        queue = review_queue([decided, pending])

        assert [v.pair_id for v in queue] == ["nfl-kc", "zzz-decided"]

    def test_ordering_within_a_group_is_stable(self) -> None:
        from arb.review import review_queue

        first = verify_candidate(replace(proposed(), pair_id="aaa"))
        second = verify_candidate(replace(proposed(), pair_id="bbb"))

        assert [v.pair_id for v in review_queue([second, first])] == ["aaa", "bbb"]


def test_review_pair_does_not_mutate_the_candidate() -> None:
    candidate = proposed()

    review_pair(candidate)

    assert candidate.status is PairStatus.PROPOSED
    assert candidate.verdict is None


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("settlement_source", "Settlement source"),
        ("settling_release", "Settling release"),
        ("postponement_rule", "Postponement"),
        ("overtime_rule", "Overtime"),
        ("threshold", "Threshold"),
        ("tie_break_rule", "Tie-break"),
    ],
)
def test_labels_read_as_contract_terms(field: str, label: str) -> None:
    assert row(review(), field).label == label


class TestOperatorLine:
    def test_an_undecided_pair_has_no_operator_line(self) -> None:
        assert review().operator_line() == ""

    def test_a_decision_names_who_made_it(self) -> None:
        from arb.registry import reject

        decided = reject(
            verify_candidate(proposed()),
            operator="max",
            at_ms=2_000,
            note="different box score feed",
        )

        assert review_pair(decided).operator_line() == (
            " by max - different box score feed"
        )
