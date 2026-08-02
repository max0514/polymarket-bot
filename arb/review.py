"""The pair review view - what the operator looks at before approving.

Pure. Turns a `PairCandidate` into a side-by-side diff of the two venues'
contract terms, so the reviewer sees *why* the rule layer reached its verdict
rather than being handed a bare pass or fail.

Two properties matter more than anything cosmetic here.

**The view and the gate cannot disagree.** Both come from the same `verify()`
call. A row is marked as agreeing only when the rule layer also thinks so, so
the operator can never be shown a green field that the gate is quietly
rejecting.

**The view offers no override.** `can_approve` is false for anything the rules
rejected. The whole point of a deterministic rule layer is that severity
judgements do not get made under time pressure, and a review screen with an
"approve anyway" button is exactly where that discipline dies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

from arb.registry import PairCandidate, PairStatus
from arb.verification import (
    ContractTerms,
    Failure,
    Verdict,
    normalise_term,
    verify,
)

__all__ = [
    "PairReview",
    "TermRow",
    "TermStatus",
    "review_pair",
    "review_queue",
]


class TermStatus(Enum):
    """Why a row looks the way it does.

    `DIFFERS` and `UNSTATED` are kept apart deliberately: "the venues disagree"
    and "nobody wrote it down" call for different follow-up, and collapsing them
    into one colour would hide which one the reviewer is looking at.
    """

    AGREES = "agrees"
    DIFFERS = "differs"
    UNSTATED = "unstated"


@dataclass(frozen=True, slots=True)
class TermRow:
    field: str
    label: str
    kalshi: str | None
    polymarket: str | None
    status: TermStatus

    @property
    def is_blocking(self) -> bool:
        return self.status is not TermStatus.AGREES


@dataclass(frozen=True, slots=True)
class PairReview:
    pair_id: str
    status: PairStatus
    kalshi_ticker: str
    polymarket_id: str
    kalshi_question: str
    polymarket_question: str
    kalshi_url: str
    polymarket_url: str
    category: str
    settlement_date: str
    model_confidence: str
    verified: bool
    failures: tuple[Failure, ...]
    rows: tuple[TermRow, ...]
    operator: str = ""
    operator_note: str = ""

    @property
    def can_approve(self) -> bool:
        """Approvable only if the rules passed *and* nobody has decided yet."""
        return self.verified and self.status in (
            PairStatus.PROPOSED,
            PairStatus.AWAITING_APPROVAL,
        )

    @property
    def awaiting_decision(self) -> bool:
        """The operator can still act on this one."""
        return self.status in (PairStatus.PROPOSED, PairStatus.AWAITING_APPROVAL)

    @property
    def blocked_by_rules(self) -> bool:
        """The rule layer rejected it, so no operator action is available.

        Still shown, because a reviewer needs to see *what* was rejected in
        order to go and fix the terms at source - but there is nothing to
        click.
        """
        return self.status is PairStatus.REJECTED_BY_RULES

    @property
    def blocking_rows(self) -> tuple[TermRow, ...]:
        return tuple(row for row in self.rows if row.is_blocking)

    def operator_line(self) -> str:
        """Who decided and why, for a pair that is already settled one way."""
        if not self.operator:
            return ""
        note = f" - {self.operator_note}" if self.operator_note else ""
        return f" by {self.operator}{note}"


#: Field, and the words a reviewer would use for it. The labels matter: someone
#: comparing contract terms under time pressure is reading the terms, not the
#: schema.
_LABELS: tuple[tuple[str, str], ...] = (
    ("settlement_source", "Settlement source"),
    ("settling_release", "Settling release"),
    ("threshold", "Threshold"),
    ("tie_break_rule", "Tie-break"),
    ("void_rule", "Void policy"),
    ("postponement_rule", "Postponement"),
    ("overtime_rule", "Overtime"),
)

_NOT_REVISABLE = "not revisable - no pin needed"


def review_pair(candidate: PairCandidate) -> PairReview:
    """Build the reviewer's view of one candidate.

    Does not mutate or advance the candidate - running `verify()` here is a
    read. Promoting a candidate is `verify_candidate`, and keeping the two
    separate means opening a review screen cannot change what is tradeable.
    """
    verdict = verify(candidate.kalshi, candidate.polymarket)
    kalshi_terms = candidate.kalshi.terms
    polymarket_terms = candidate.polymarket.terms

    rows = tuple(
        _term_row(field, label, kalshi_terms, polymarket_terms)
        for field, label in _LABELS
    ) + (_release_row(kalshi_terms, polymarket_terms, verdict),)

    return PairReview(
        pair_id=candidate.pair_id,
        status=candidate.status,
        kalshi_ticker=candidate.kalshi.series_ticker,
        polymarket_id=candidate.polymarket.condition_id,
        kalshi_question=candidate.kalshi.title,
        polymarket_question=candidate.polymarket.question,
        kalshi_url=candidate.kalshi.contract_terms_url,
        polymarket_url=candidate.polymarket.resolution_source_url,
        category=candidate.category,
        settlement_date=candidate.settlement_date,
        model_confidence=f"{candidate.model_confidence:.2f}",
        verified=verdict.verified,
        failures=verdict.failures,
        rows=rows,
        operator=candidate.operator,
        operator_note=candidate.operator_note,
    )


def review_queue(candidates: Iterable[PairCandidate]) -> Sequence[PairReview]:
    """The reviewer's work list: undecided pairs first, then everything else.

    Ties break on pair id, so the queue does not reshuffle between refreshes
    and the reviewer does not lose their place.
    """
    views = [review_pair(candidate) for candidate in candidates]
    return sorted(views, key=lambda view: (_tier(view), view.pair_id))


def _tier(view: PairReview) -> int:
    """Actionable first, then rules-blocked, then settled history."""
    if view.awaiting_decision:
        return 0
    if view.blocked_by_rules:
        return 1
    return 2


def _term_row(
    field: str, label: str, kalshi: ContractTerms, polymarket: ContractTerms
) -> TermRow:
    left = getattr(kalshi, field)
    right = getattr(polymarket, field)
    return TermRow(
        field=field,
        label=label,
        kalshi=left,
        polymarket=right,
        status=_status(left, right),
    )


def _release_row(
    kalshi: ContractTerms, polymarket: ContractTerms, verdict: Verdict
) -> TermRow:
    """Release pinning, expressed the way a reviewer thinks about it.

    A non-revisable source shows a sentence rather than a blank, because a blank
    cell reads as missing data when it is actually a real answer.
    """
    release_failures = {
        Failure.RELEASE_NOT_PINNED,
        Failure.DIVERGENT_RELEASE_TIMESTAMP,
        Failure.DIVERGENT_REVISABILITY,
    }
    failed = bool(release_failures & set(verdict.failures))

    return TermRow(
        field="settling_release_timestamp",
        label="Release pinned to",
        kalshi=_release_value(kalshi),
        polymarket=_release_value(polymarket),
        status=(
            TermStatus.AGREES
            if not failed
            else (
                TermStatus.UNSTATED
                if Failure.RELEASE_NOT_PINNED in verdict.failures
                else TermStatus.DIFFERS
            )
        ),
    )


def _release_value(terms: ContractTerms) -> str | None:
    if not terms.revisable:
        return _NOT_REVISABLE
    return terms.settling_release_timestamp


def _status(left: str | None, right: str | None) -> TermStatus:
    # Normalised by the gate's own helper, not a copy of it. Two
    # implementations would drift, and the moment they did the operator would
    # be shown a green row for a term the rule layer was rejecting.
    normalised_left = normalise_term(left)
    normalised_right = normalise_term(right)
    if normalised_left is None or normalised_right is None:
        return TermStatus.UNSTATED
    return (
        TermStatus.AGREES if normalised_left == normalised_right else TermStatus.DIFFERS
    )
