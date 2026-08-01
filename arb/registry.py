"""The Pair Registry and the propose / verify / approve workflow.

Runs outside the reducer, on its own clock, publishing a registry the reducer
reads as plain data. Keeping it out of the reducer matters because pair
matching is slow, model-assisted, and human-gated, and none of those belong on
a path that has to be deterministic and replayable.

Authority runs in one direction. The model proposes and records a confidence,
but cannot promote a candidate. The rule layer can only reject. The operator
decides, but only among candidates the rules already passed. That ordering is
what stops a persuasive model or a hurried operator from putting an unverified
pair in front of capital.

Every stage is recorded, because the resulting table - model confidence, rule
verdict, operator decision, and eventually post-settlement ground truth - is
the calibration dataset a later autonomous phase would be justified by. It
cannot be reconstructed after the fact, so it is written from the start.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Iterable, Mapping

from arb.canonical import canonical_decimal
from arb.domain import MatchedPair
from arb.verification import KalshiSeries, PolymarketMarket, Verdict, verify

__all__ = [
    "PairCandidate",
    "PairStatus",
    "approve",
    "propose",
    "registry_from",
    "reject",
    "revoke",
    "verify_candidate",
]


class PairStatus(Enum):
    PROPOSED = "proposed"
    REJECTED_BY_RULES = "rejected_by_rules"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED_BY_OPERATOR = "rejected_by_operator"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class PairCandidate:
    """A proposed pair at whatever stage of the workflow it has reached."""

    pair_id: str
    kalshi: KalshiSeries
    polymarket: PolymarketMarket
    category: str
    settlement_date: str

    #: What the proposing model thought, recorded before anything checks it.
    model_confidence: Decimal
    proposed_at_ms: int

    status: PairStatus = PairStatus.PROPOSED
    verdict: Verdict | None = None

    operator: str = ""
    operator_note: str = ""
    decided_at_ms: int | None = None

    #: Post-settlement ground truth: did both venues actually pay identically?
    #: `None` until the pair has settled on both sides.
    settled_identically: bool | None = None

    @property
    def is_tradeable(self) -> bool:
        return self.status is PairStatus.APPROVED

    def as_matched_pair(self) -> MatchedPair:
        """The reducer's view of an approved pair.

        The settlement source is taken from the Kalshi terms, which is safe
        only because verification has already established that both venues
        state the same one.
        """
        source = self.kalshi.terms.settlement_source or ""
        return MatchedPair(
            pair_id=self.pair_id,
            kalshi_contract_id=self.kalshi.series_ticker,
            polymarket_contract_id=self.polymarket.condition_id,
            category=self.category,
            settlement_source=source,
            settlement_date=self.settlement_date,
        )

    def as_record(self) -> dict[str, str]:
        """One row of the calibration dataset."""
        return {
            "pair_id": self.pair_id,
            "kalshi_series": self.kalshi.series_ticker,
            "polymarket_condition_id": self.polymarket.condition_id,
            "category": self.category,
            "settlement_date": self.settlement_date,
            "model_confidence": canonical_decimal(self.model_confidence),
            "proposed_at_ms": str(self.proposed_at_ms),
            "status": self.status.value,
            "rule_verified": _verified_flag(self.verdict),
            "rule_failures": (
                ",".join(f.value for f in self.verdict.failures)
                if self.verdict
                else ""
            ),
            "operator": self.operator,
            "operator_note": self.operator_note,
            "decided_at_ms": (
                str(self.decided_at_ms) if self.decided_at_ms is not None else ""
            ),
            "settled_identically": _bool_flag(self.settled_identically),
        }


def propose(
    *,
    pair_id: str,
    kalshi: KalshiSeries,
    polymarket: PolymarketMarket,
    category: str,
    settlement_date: str,
    model_confidence: Decimal,
    proposed_at_ms: int,
) -> PairCandidate:
    """Record a model's proposal. Generator only; carries no authority."""
    return PairCandidate(
        pair_id=pair_id,
        kalshi=kalshi,
        polymarket=polymarket,
        category=category,
        settlement_date=settlement_date,
        model_confidence=model_confidence,
        proposed_at_ms=proposed_at_ms,
    )


def verify_candidate(candidate: PairCandidate) -> PairCandidate:
    """Run the deterministic rule layer and route the candidate accordingly."""
    verdict = verify(candidate.kalshi, candidate.polymarket)
    return replace(
        candidate,
        verdict=verdict,
        status=(
            PairStatus.AWAITING_APPROVAL
            if verdict.verified
            else PairStatus.REJECTED_BY_RULES
        ),
    )


def approve(candidate: PairCandidate, *, operator: str, at_ms: int) -> PairCandidate:
    """Operator sign-off, after which the pair trades automatically."""
    _require(candidate, PairStatus.AWAITING_APPROVAL, "approve")
    return replace(
        candidate,
        status=PairStatus.APPROVED,
        operator=operator,
        decided_at_ms=at_ms,
    )


def reject(
    candidate: PairCandidate, *, operator: str, at_ms: int, note: str = ""
) -> PairCandidate:
    _require(candidate, PairStatus.AWAITING_APPROVAL, "reject")
    return replace(
        candidate,
        status=PairStatus.REJECTED_BY_OPERATOR,
        operator=operator,
        operator_note=note,
        decided_at_ms=at_ms,
    )


def revoke(candidate: PairCandidate, *, at_ms: int, note: str = "") -> PairCandidate:
    """Remove an approved pair from trading immediately."""
    _require(candidate, PairStatus.APPROVED, "revoke")
    return replace(
        candidate,
        status=PairStatus.REVOKED,
        operator_note=note,
        decided_at_ms=at_ms,
    )


def registry_from(candidates: Iterable[PairCandidate]) -> Mapping[str, MatchedPair]:
    """The Pair Registry: approved candidates only, in a stable order.

    Sorted by pair id because this mapping seeds the reducer's iteration order,
    and an action trace that depends on insertion order is not replayable.
    """
    approved = sorted(
        (candidate for candidate in candidates if candidate.is_tradeable),
        key=lambda candidate: candidate.pair_id,
    )
    return {
        candidate.pair_id: candidate.as_matched_pair() for candidate in approved
    }


def _require(candidate: PairCandidate, expected: PairStatus, action: str) -> None:
    if candidate.status is not expected:
        raise ValueError(
            f"cannot {action} a pair with status {candidate.status.value}; "
            f"expected {expected.value}"
        )


def _verified_flag(verdict: Verdict | None) -> str:
    if verdict is None:
        return ""
    return "1" if verdict.verified else "0"


def _bool_flag(value: bool | None) -> str:
    if value is None:
        return ""
    return "1" if value else "0"
