"""Canonical serialisation for decimal quantities.

`Decimal` preserves the scale of its operands, so `Decimal("0.07") * Decimal(
"0.10") * Decimal("0.90")` renders as `0.006300` while an equal value computed
from differently-written inputs renders as `0.0063`. The values compare equal
but the strings do not, which would make a replayed action trace differ from
the recorded one for no reason other than input formatting.

Everything written to a Decision Record, an action trace, or a report therefore
goes through `canonical_decimal`, which pins both the scale and the rounding
mode.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

__all__ = ["SCALE", "canonical_decimal", "quantize"]

# Eight decimal places on a per-contract dollar amount is sub-micro-cent -
# far below any economically meaningful threshold, and stable to serialise.
SCALE = Decimal("0.00000001")


def quantize(value: Decimal) -> Decimal:
    """Round to the canonical scale, banker's rounding."""
    return value.quantize(SCALE, rounding=ROUND_HALF_EVEN)


def canonical_decimal(value: Decimal) -> str:
    """The one true string form of a decimal quantity in this system."""
    return str(quantize(value))
