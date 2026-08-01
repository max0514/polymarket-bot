"""Canonical decimal serialisation.

Load-bearing for replay: a recorded action trace and a replayed one are
compared as text, so two spellings of the same number are a false difference.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from arb.canonical import canonical_decimal


class TestCanonicalDecimal:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Decimal("0"), "0.00000000"),
            (Decimal("0.0063"), "0.00630000"),
            (Decimal("0.006300"), "0.00630000"),
            (Decimal("-1.5"), "-1.50000000"),
            (Decimal("1E+2"), "100.00000000"),
        ],
    )
    def test_renders_fixed_point_at_a_fixed_scale(
        self, value: Decimal, expected: str
    ) -> None:
        assert canonical_decimal(value) == expected

    def test_zero_does_not_render_in_scientific_notation(self) -> None:
        """`Decimal("0").quantize(...)` is `0E-8`, whose `str()` would put an
        exponent into the decision log."""
        assert "E" not in canonical_decimal(Decimal("0"))

    def test_equal_values_written_differently_render_identically(self) -> None:
        assert canonical_decimal(Decimal("0.1")) == canonical_decimal(
            Decimal("0.10000")
        )

    def test_rounding_is_half_even_so_it_does_not_drift_upward(self) -> None:
        """Banker's rounding: a log that always rounds up accumulates a bias
        into every aggregate computed from it."""
        assert canonical_decimal(Decimal("0.000000005")) == "0.00000000"
        assert canonical_decimal(Decimal("0.000000015")) == "0.00000002"
