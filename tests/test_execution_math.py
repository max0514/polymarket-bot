"""The two pure calculations behind sequential legging.

Both are separated from the reducer because they are arithmetic with an exact
answer, and asserting on them through an event sequence would obscure what is
actually being checked.
"""

from __future__ import annotations

from decimal import Decimal

from arb.execution import breakeven_price, leg_difficulty
from arb.pricing import net_edge
from tests.builders import SPORTS_FEES


# The bound is a root of a quadratic and is generally irrational, so "breaks
# even" is checked to within far more precision than a venue's tick size.
EXACT = Decimal("1e-20")


class TestBreakevenPrice:
    def test_the_returned_price_is_breakeven(self) -> None:
        """The defining property, checked against `net_edge` itself: at the
        returned price the pair earns nothing, and a hair above it loses."""
        breakeven = breakeven_price(
            filled_leg_price=Decimal("0.10"), filled_venue="kalshi", schedule=SPORTS_FEES
        )

        assert abs(net_edge(Decimal("0.10"), breakeven, SPORTS_FEES)) < EXACT
        assert net_edge(
            Decimal("0.10"), breakeven + Decimal("0.0001"), SPORTS_FEES
        ) < 0

    def test_anything_under_the_bound_is_profitable(self) -> None:
        breakeven = breakeven_price(
            filled_leg_price=Decimal("0.10"), filled_venue="kalshi", schedule=SPORTS_FEES
        )

        assert net_edge(
            Decimal("0.10"), breakeven - Decimal("0.0001"), SPORTS_FEES
        ) > 0

    def test_a_worse_leg_one_fill_tightens_the_leg_two_bound(self) -> None:
        """A moved market is salvaged only up to the point where salvage stops
        being profitable, and that point moves with leg 1's actual fill."""
        good_fill = breakeven_price(
            filled_leg_price=Decimal("0.10"), filled_venue="kalshi", schedule=SPORTS_FEES
        )
        bad_fill = breakeven_price(
            filled_leg_price=Decimal("0.13"), filled_venue="kalshi", schedule=SPORTS_FEES
        )

        assert bad_fill < good_fill

    def test_it_works_from_either_leg(self) -> None:
        """Whichever leg filled first, the bound applies to the other one."""
        from_polymarket = breakeven_price(
            filled_leg_price=Decimal("0.88"),
            filled_venue="polymarket",
            schedule=SPORTS_FEES,
        )

        assert abs(net_edge(from_polymarket, Decimal("0.88"), SPORTS_FEES)) < EXACT

    def test_a_leg_one_fill_leaving_no_room_yields_a_bound_at_or_below_zero(
        self,
    ) -> None:
        """Nothing can be bought at a non-positive price, so this is how
        "unwind instead" gets expressed."""
        assert breakeven_price(
            filled_leg_price=Decimal("1.00"), filled_venue="kalshi", schedule=SPORTS_FEES
        ) <= 0

    def test_a_deep_tail_fill_still_leaves_a_thin_but_real_bound(self) -> None:
        """Worth pinning: at p1=0.99 the pair is not dead, it just needs the
        other leg under a cent. Treating that as hopeless would abandon
        salvageable trades."""
        bound = breakeven_price(
            filled_leg_price=Decimal("0.99"), filled_venue="kalshi", schedule=SPORTS_FEES
        )

        assert Decimal("0") < bound < Decimal("0.01")


class TestLegDifficulty:
    def test_a_thinner_book_is_harder(self) -> None:
        """User story 37: difficulty is measured from depth at the target price
        relative to intended size, not assumed."""
        thin = leg_difficulty(depth=50, intended_size=100, latency_ms=100)
        deep = leg_difficulty(depth=500, intended_size=100, latency_ms=100)

        assert thin > deep

    def test_a_slower_venue_is_harder_at_equal_depth(self) -> None:
        slow = leg_difficulty(depth=100, intended_size=100, latency_ms=900)
        fast = leg_difficulty(depth=100, intended_size=100, latency_ms=100)

        assert slow > fast

    def test_a_book_that_cannot_fill_the_size_is_hardest(self) -> None:
        assert leg_difficulty(depth=0, intended_size=100, latency_ms=0) > leg_difficulty(
            depth=1, intended_size=100, latency_ms=10_000
        )
