"""Inventory-aware pair selection - steering capital Drift rather than fighting it.

Both legs are paid on entry; the payout lands entirely at whichever venue holds
the winning side. Rebalancing between venues is slow, costly, and the
highest-scrutiny operation available, which makes rebalancing frequency and
account-detection exposure the same variable. Choosing pairs well is the cheap
lever; wiring money between venues is the expensive one.

**What can actually be steered.** Buying a contract at price `p` costs `p` now
and returns `1` with probability approximately `p`, so the *expected* balance
change at either venue is zero and expectation offers no lever at all. What
differs between candidates is the *probability* that a lumpy, all-or-nothing
payout lands where it is needed. Buying the favourite on the depleted venue
makes replenishment likely without making it certain, and that probability is
the whole of the mechanism. Drift is a random walk; this tilts it, and the
tilt is worth having precisely because the walk grows as the square root of
trade count and a balance floor is a hard stop.

**When it applies.** Steering costs profit, so it stays off until a venue is
actually near trouble - inside `steering_balance_band`. Outside the band,
candidates rank by total net profit alone.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from arb.decisions import DecisionRecord
from arb.domain import VENUES, Venue
from arb.risk import RiskLimits

__all__ = ["rank_candidates", "replenishment_probability"]

ZERO = Decimal("0")


def replenishment_probability(
    *,
    kalshi_price: Decimal,
    polymarket_price: Decimal,
    venue_balances: Mapping[Venue, Decimal],
) -> Decimal:
    """The chance this pair's payout lands at the more depleted venue.

    A binary bought at `p` wins with probability approximately `p`, so the
    depleted venue's own leg price *is* the replenishment probability. Returns
    zero when the venues are level and there is nothing to steer toward.
    """
    kalshi = venue_balances.get("kalshi", ZERO)
    polymarket = venue_balances.get("polymarket", ZERO)
    if kalshi == polymarket:
        return ZERO
    return kalshi_price if kalshi < polymarket else polymarket_price


def rank_candidates(
    candidates: Iterable[DecisionRecord],
    *,
    venue_balances: Mapping[Venue, Decimal],
    limits: RiskLimits,
) -> Sequence[DecisionRecord]:
    """Order acceptable candidates best-first.

    Ranked by total net profit rather than edge percentage, so that a thin
    high-percentage opportunity does not outrank a fillable one. Inside the
    steering band, replenishment outranks profit; outside it, profit alone
    decides. Pair id breaks remaining ties, because the resulting order reaches
    the action trace and a trace that varies between runs is not replayable.
    """
    steering = _is_steering(venue_balances, limits)

    def sort_key(candidate: DecisionRecord) -> tuple[Decimal, Decimal, str]:
        profit = candidate.expected_profit or ZERO
        replenishment = (
            replenishment_probability(
                kalshi_price=candidate.kalshi_price or ZERO,
                polymarket_price=candidate.polymarket_price or ZERO,
                venue_balances=venue_balances,
            )
            if steering
            else ZERO
        )
        # Negated so that `sorted` ascending puts the best first while the
        # pair id tiebreak stays in natural ascending order.
        return (-replenishment, -profit, candidate.pair_id)

    return sorted(candidates, key=sort_key)


def _is_steering(
    venue_balances: Mapping[Venue, Decimal], limits: RiskLimits
) -> bool:
    """True when either venue is inside the band where Drift needs steering."""
    if limits.steering_balance_band <= ZERO:
        return False
    return any(
        venue_balances.get(venue, ZERO) < limits.steering_balance_band
        for venue in VENUES
    )

