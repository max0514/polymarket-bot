"""The arithmetic behind sequential legging.

Two calculations, both pure:

* `leg_difficulty` decides which leg goes first. Sequential legging opens an
  exposure window between the two fills, and taking the harder side first means
  the window only opens once the difficult side has already succeeded.
* `breakeven_price` bounds the second leg. When the market moves between fills,
  the choice is not "abandon or chase" - it is chase up to the price at which
  the pair stops being profitable, and unwind beyond it.
"""

from __future__ import annotations

from decimal import Decimal

from arb.domain import Venue
from arb.pricing import FeeSchedule

__all__ = ["breakeven_price", "leg_difficulty"]

ONE = Decimal("1")
ZERO = Decimal("0")

#: Latency at which a venue is considered to contribute one book's worth of
#: difficulty. A scaling constant for a heuristic, not a measured quantity.
REFERENCE_LATENCY_MS = Decimal("1000")


def breakeven_price(
    *, filled_leg_price: Decimal, filled_venue: Venue, schedule: FeeSchedule
) -> Decimal:
    """The highest price for the *other* leg at which the pair still breaks even.

    Solving `1 - p1 - p2 - fees(p1, p2) = 0` for `p2`. Because the fee on the
    unfilled leg is itself `r * p2 * (1 - p2)`, this is a quadratic rather than
    a subtraction:

        r*p2^2 - (1 + r)*p2 + C = 0,  where C = 1 - p1 - r1*p1*(1 - p1)

    The parabola opens upward and its larger root always exceeds 1, so the
    feasible region is everything at or below the smaller root - which is the
    bound returned here.

    A non-positive result means leg 1 filled so badly that no price can rescue
    the pair. Nothing is buyable at zero, so the caller reads that as "unwind".
    """
    if filled_venue == "kalshi":
        filled_rate, open_rate = schedule.kalshi_rate, schedule.polymarket_rate
    else:
        filled_rate, open_rate = schedule.polymarket_rate, schedule.kalshi_rate

    remaining = (
        ONE - filled_leg_price - filled_rate * filled_leg_price * (ONE - filled_leg_price)
    )

    if open_rate == 0:
        return remaining

    b = ONE + open_rate
    discriminant = b * b - 4 * open_rate * remaining
    if discriminant < 0:
        # No real root: the fee curve alone exceeds what is left. Unreachable
        # for any rate a venue would plausibly charge, but a negative bound is
        # the honest answer rather than a crash.
        return ZERO - ONE
    return (b - discriminant.sqrt()) / (2 * open_rate)


def leg_difficulty(*, depth: int, intended_size: int, latency_ms: int) -> Decimal:
    """How hard this leg is expected to be to fill. Higher is harder.

    Combines the two measurable things the spec names: available depth at the
    target price relative to intended size, and measured venue latency. The
    weighting between them is a heuristic - what matters is that both move the
    score in the right direction and that neither is assumed.
    """
    latency_factor = ONE + Decimal(latency_ms) / REFERENCE_LATENCY_MS
    if depth <= 0:
        # A book that cannot fill any of the size is maximally hard, and must
        # outrank any finite score however slow the other venue is.
        return Decimal("Infinity")
    return (Decimal(intended_size) / Decimal(depth)) * latency_factor
