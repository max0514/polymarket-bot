"""Decision Records - the logged evaluation of a candidate, accepted or rejected.

This is the deliverable. The system this replaces persisted only detected
opportunities, so it had a numerator and no denominator and could not produce a
base rate. Here every evaluation is written, and the rejection reason
distinguishes "no opportunity existed" from "the system filtered it out"
(user stories 1 and 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from arb.canonical import canonical_decimal
from arb.pricing import FeeBreakdown

__all__ = ["DecisionRecord", "RejectionReason"]


class RejectionReason(Enum):
    """Why a candidate was not traded.

    Ordered roughly by how early in evaluation the gate sits, because the
    verdict report reads this as a funnel.
    """

    #: One or both venues had no book, or no offer on the side we buy.
    EMPTY_BOOK = "empty_book"
    #: Cross-venue Skew exceeded the threshold; a stale book cannot be traded
    #: through, and evaluating it would manufacture a phantom edge.
    EXCESSIVE_SKEW = "excessive_skew"
    #: One or both books were older than `max_book_age_ms`.
    STALE_BOOK = "stale_book"
    #: No fee schedule configured for this category, so the candidate cannot be
    #: priced honestly. Fails closed rather than guessing a rate.
    UNPRICEABLE_CATEGORY = "unpriceable_category"
    #: Net Edge at or below zero: the fee hurdle exceeds the price gap.
    NEGATIVE_NET_EDGE = "negative_net_edge"
    #: Net Edge positive but under the configured minimum. Distinguished from
    #: the above so the funnel can count "positive after fees" separately from
    #: "worth trading".
    BELOW_MIN_NET_EDGE = "below_min_net_edge"
    #: Sizing walked the books and found no contract worth taking.
    NO_PROFITABLE_SIZE = "no_profitable_size"
    #: A risk flag or budget blocked entry. The specific flag is recorded in
    #: `blocking_flags`.
    RISK_BLOCKED = "risk_blocked"
    #: The kill switch is stopping new entries.
    KILL_SWITCH = "kill_switch"


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One evaluation of one Matched Pair at one moment."""

    pair_id: str
    category: str
    settlement_source: str
    settlement_date: str
    evaluated_at_ms: int
    accepted: bool
    rejection_reason: RejectionReason | None = None

    kalshi_price: Decimal | None = None
    polymarket_price: Decimal | None = None
    gross_edge: Decimal | None = None
    net_edge: Decimal | None = None
    fees: FeeBreakdown | None = None

    #: Freshness of the two books at evaluation, so a later reader can tell a
    #: real edge from one manufactured by a lagging venue.
    skew_ms: int | None = None
    kalshi_book_age_ms: int | None = None
    polymarket_book_age_ms: int | None = None

    #: Depth available at the evaluated prices, and the size the marginal-stop
    #: walk selected. `size` is 0 for every rejection.
    kalshi_top_size: int | None = None
    polymarket_top_size: int | None = None
    size: int = 0
    expected_profit: Decimal | None = None

    #: Risk flags that blocked this candidate, if any.
    blocking_flags: tuple[str, ...] = ()

    def as_record(self) -> dict[str, str]:
        """Flat, string-valued row for cross-pair storage and analysis.

        Flat because the spec asks for storage keyed for cross-pair analysis
        rather than per-instrument files, and string-valued because
        `canonical_decimal` is what keeps a replayed trace byte-identical.
        """
        row = {
            "pair_id": self.pair_id,
            "category": self.category,
            "settlement_source": self.settlement_source,
            "settlement_date": self.settlement_date,
            "evaluated_at_ms": str(self.evaluated_at_ms),
            "accepted": "1" if self.accepted else "0",
            "rejection_reason": (
                self.rejection_reason.value if self.rejection_reason else ""
            ),
            "kalshi_price": _decimal_or_blank(self.kalshi_price),
            "polymarket_price": _decimal_or_blank(self.polymarket_price),
            "gross_edge": _decimal_or_blank(self.gross_edge),
            "net_edge": _decimal_or_blank(self.net_edge),
            "expected_profit": _decimal_or_blank(self.expected_profit),
            "skew_ms": _int_or_blank(self.skew_ms),
            "kalshi_book_age_ms": _int_or_blank(self.kalshi_book_age_ms),
            "polymarket_book_age_ms": _int_or_blank(self.polymarket_book_age_ms),
            "kalshi_top_size": _int_or_blank(self.kalshi_top_size),
            "polymarket_top_size": _int_or_blank(self.polymarket_top_size),
            "size": str(self.size),
            "blocking_flags": ",".join(self.blocking_flags),
        }
        fee_fields = self.fees.as_record() if self.fees else {}
        for name in (
            "kalshi_rate",
            "polymarket_rate",
            "kalshi_fee",
            "polymarket_fee",
            "total",
        ):
            row[f"fee_{name}"] = fee_fields.get(name, "")
        return row


def _decimal_or_blank(value: Decimal | None) -> str:
    return canonical_decimal(value) if value is not None else ""


def _int_or_blank(value: int | None) -> str:
    return str(value) if value is not None else ""
