"""Seam 2 - deterministic pair verification.

`verify(kalshi_series, polymarket_market) -> Verdict`. Pure: no I/O, no clock,
no model. The proposing model has no authority here; this layer either finds
mechanical identity in the two contracts' terms or it rejects.

Two properties do the real work:

* **Fails closed.** A term that is not stated on both sides is not "probably
  the same", it is unverifiable, and unverifiable rejects. Silence is not
  agreement.
* **Any divergence rejects outright.** No severity judgement, because severity
  judgements get made under time pressure and get made wrong.

Comparison is exact after conservative normalisation. Case and surrounding
whitespace are presentation; anything beyond that is a difference in terms, and
resolving whether two differently-worded rules mean the same thing is inference,
which is what this layer exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ContractTerms",
    "Failure",
    "KalshiSeries",
    "PolymarketMarket",
    "OPTIONAL_FIELDS",
    "Verdict",
    "normalise_term",
    "verify",
]


class Failure(Enum):
    """Why a proposed pair was not verified."""

    UNVERIFIABLE_SETTLEMENT_SOURCE = "unverifiable_settlement_source"
    UNVERIFIABLE_SETTLING_RELEASE = "unverifiable_settling_release"
    UNVERIFIABLE_VOID_RULE = "unverifiable_void_rule"
    UNVERIFIABLE_POSTPONEMENT_RULE = "unverifiable_postponement_rule"
    UNVERIFIABLE_OVERTIME_RULE = "unverifiable_overtime_rule"
    UNVERIFIABLE_THRESHOLD = "unverifiable_threshold"
    UNVERIFIABLE_TIE_BREAK_RULE = "unverifiable_tie_break_rule"

    DIVERGENT_SETTLEMENT_SOURCE = "divergent_settlement_source"
    DIVERGENT_SETTLING_RELEASE = "divergent_settling_release"
    DIVERGENT_VOID_RULE = "divergent_void_rule"
    DIVERGENT_POSTPONEMENT_RULE = "divergent_postponement_rule"
    DIVERGENT_OVERTIME_RULE = "divergent_overtime_rule"
    DIVERGENT_THRESHOLD = "divergent_threshold"
    DIVERGENT_TIE_BREAK_RULE = "divergent_tie_break_rule"

    #: The two sides disagree about whether the settling source publishes
    #: revisions at all.
    DIVERGENT_REVISABILITY = "divergent_revisability"
    #: A revisable source with no pinned release timestamp: a later revision
    #: could split a pair that agreed at publication.
    RELEASE_NOT_PINNED = "release_not_pinned"
    DIVERGENT_RELEASE_TIMESTAMP = "divergent_release_timestamp"


@dataclass(frozen=True, slots=True)
class ContractTerms:
    """The machine-checkable part of a contract's settlement rules.

    Every string field is optional because the real question this type asks is
    "did the venue actually state this?" - and `None` is the answer that makes
    the pair unverifiable rather than matched.
    """

    settlement_source: str | None
    settling_release: str | None
    settling_release_timestamp: str | None
    revisable: bool
    void_rule: str | None
    postponement_rule: str | None
    overtime_rule: str | None
    threshold: str | None
    tie_break_rule: str | None


@dataclass(frozen=True, slots=True)
class KalshiSeries:
    """A Kalshi contract family, carrying structured settlement sources and a
    contract-terms document."""

    series_ticker: str
    title: str
    contract_terms_url: str
    terms: ContractTerms
    #: The venue's resolution language, verbatim. Never machine-compared - the
    #: structured fields in `terms` are the comparison - but shown to the
    #: reviewer, whose job is to catch what the extraction missed.
    resolution_text: str = ""
    #: The tradeable event page - where the market itself is listed, as opposed
    #: to the terms document behind it.
    event_url: str = ""


@dataclass(frozen=True, slots=True)
class PolymarketMarket:
    condition_id: str
    question: str
    resolution_source_url: str
    terms: ContractTerms
    #: See `KalshiSeries.resolution_text`.
    resolution_text: str = ""
    #: See `KalshiSeries.event_url`.
    event_url: str = ""


@dataclass(frozen=True, slots=True)
class Verdict:
    verified: bool
    failures: tuple[Failure, ...]

    def as_record(self) -> dict[str, str]:
        return {
            "verified": "1" if self.verified else "0",
            "failures": ",".join(failure.value for failure in self.failures),
        }


#: Fields compared one for one, with the failure raised for each outcome and
#: whether silence on BOTH sides is tolerable. Declared as data so that adding
#: a term is one line and cannot skip either check.
#:
#: The `required` flag is a deliberate refinement of the original blanket
#: rule. For core fields, mutual silence means nothing was checked and the
#: pair rejects. For overtime and tie-break, mutual silence usually means the
#: concept does not exist in the sport (baseball cannot tie; extra innings are
#: simply part of the result), and rejecting on it would make every real pair
#: fail closed - turning the operator override into the normal path and
#: destroying its signal. One-sided silence still rejects, and stated-but-
#: different still rejects, on every field.
#:
#: MLB-ONLY ASSUMPTION, confirmed by the operator (2026-08-06): this global
#: relaxation is safe because the collected universe is MLB, where ties do not
#: exist. Before adding a league where they do (NFL regular-season games can
#: tie), make optionality category-aware - mutual silence on tie-break for an
#: NFL pair would be a real unchecked gap, not inapplicability.
_COMPARED_FIELDS: tuple[tuple[str, Failure, Failure, bool], ...] = (
    (
        "settlement_source",
        Failure.UNVERIFIABLE_SETTLEMENT_SOURCE,
        Failure.DIVERGENT_SETTLEMENT_SOURCE,
        True,
    ),
    (
        "settling_release",
        Failure.UNVERIFIABLE_SETTLING_RELEASE,
        Failure.DIVERGENT_SETTLING_RELEASE,
        True,
    ),
    ("void_rule", Failure.UNVERIFIABLE_VOID_RULE, Failure.DIVERGENT_VOID_RULE, True),
    (
        "postponement_rule",
        Failure.UNVERIFIABLE_POSTPONEMENT_RULE,
        Failure.DIVERGENT_POSTPONEMENT_RULE,
        True,
    ),
    (
        "overtime_rule",
        Failure.UNVERIFIABLE_OVERTIME_RULE,
        Failure.DIVERGENT_OVERTIME_RULE,
        False,
    ),
    ("threshold", Failure.UNVERIFIABLE_THRESHOLD, Failure.DIVERGENT_THRESHOLD, True),
    (
        "tie_break_rule",
        Failure.UNVERIFIABLE_TIE_BREAK_RULE,
        Failure.DIVERGENT_TIE_BREAK_RULE,
        False,
    ),
)

#: Field names where mutual silence passes - shared with the review view so
#: the screen and the gate cannot disagree about what a blank row means.
OPTIONAL_FIELDS: frozenset[str] = frozenset(
    name for name, _, _, required in _COMPARED_FIELDS if not required
)


def verify(kalshi: KalshiSeries, polymarket: PolymarketMarket) -> Verdict:
    """Compare two contracts' terms and report every reason they do not match.

    Every failure is collected rather than short-circuiting on the first, so a
    reviewer sees the whole diff in one pass instead of one per review cycle.
    """
    failures: list[Failure] = []

    for field, unverifiable, divergent, required in _COMPARED_FIELDS:
        left = normalise_term(getattr(kalshi.terms, field))
        right = normalise_term(getattr(polymarket.terms, field))
        if left is None and right is None:
            if required:
                failures.append(unverifiable)
        elif left is None or right is None:
            failures.append(unverifiable)
        elif left != right:
            failures.append(divergent)

    failures.extend(_release_failures(kalshi.terms, polymarket.terms))

    # Ordered by the enum's own declaration order so that the same verdict
    # always serialises identically, whatever order the checks ran in.
    ordered = tuple(sorted(set(failures), key=list(Failure).index))
    return Verdict(verified=not ordered, failures=ordered)


def _release_failures(
    kalshi: ContractTerms, polymarket: ContractTerms
) -> list[Failure]:
    """Revision handling.

    A source that publishes revisions can retroactively disagree with itself,
    which splits a pair that agreed at publication. Such a pair is verifiable
    only when both venues pin the same release timestamp.
    """
    if kalshi.revisable != polymarket.revisable:
        return [Failure.DIVERGENT_REVISABILITY]
    if not kalshi.revisable:
        return []

    left = normalise_term(kalshi.settling_release_timestamp)
    right = normalise_term(polymarket.settling_release_timestamp)
    if left is None or right is None:
        return [Failure.RELEASE_NOT_PINNED]
    if left != right:
        return [Failure.DIVERGENT_RELEASE_TIMESTAMP]
    return []


def normalise_term(value: str | None) -> str | None:
    """Normalise a term for comparison, or `None` if the venue did not state it.

    Whitespace-only is treated as unstated: an empty field in a contract-terms
    document says nothing, and treating it as a value that happens to match
    another empty field would let two silences verify as agreement.
    """
    if value is None:
        return None
    normalised = " ".join(value.split()).casefold()
    return normalised or None
