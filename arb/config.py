"""Tunable thresholds.

The spec proposes no defaults for these - capital and verdict criteria were
left open - so they are configuration with no values baked into the core. The
config travels inside `State` so that a replayed event log reproduces its
original decisions rather than re-deciding under today's settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from arb.pricing import FeeSchedule
from arb.risk import RiskLimits

__all__ = ["Config"]


@dataclass(frozen=True, slots=True)
class Config:
    #: Per-category fee schedules. Polymarket's rate is category-dependent, so
    #: it is resolved per market rather than averaged into one wrong constant
    #: (user story 26).
    fee_schedules: Mapping[str, FeeSchedule] = field(
        default_factory=lambda: MappingProxyType({})
    )

    #: Minimum Net Edge in currency per contract, so the threshold is
    #: comparable against the fee hurdle rather than against a raw price gap
    #: (user story 29).
    min_net_edge: Decimal = Decimal("0")

    #: Maximum tolerated cross-venue Skew before a pair is rejected unevaluated.
    max_skew_ms: int = 2_000

    #: Maximum age of either book, measured against the reducer's latest known
    #: time, before it is considered stale.
    max_book_age_ms: int = 5_000

    #: How long an entry may sit unanswered before it is unwound. Unlike the
    #: capital parameters, this one has a default: every other way out of the
    #: exposure window needs a venue message, so with no timeout a silent venue
    #: strands the pair permanently. Thirty seconds is generous for a taker
    #: round trip. Zero disables the sweep.
    entry_timeout_ms: int = 30_000

    #: Balance floors, concentration caps, and the Leg Failure budget. All
    #: default to no limit - the spec proposes no values because total capital
    #: was never specified.
    risk: RiskLimits = field(default_factory=RiskLimits)

    def fees_for(self, category: str) -> FeeSchedule | None:
        """`None` when the category has no configured schedule.

        Fails closed at the call site: an unpriceable candidate is rejected and
        recorded, never silently priced with a guessed rate.
        """
        return self.fee_schedules.get(category)
