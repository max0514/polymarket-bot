"""Sequential legging, at the reducer seam.

Taker on both sides, harder leg first, so the exposure window is a single round
trip and only opens once the difficult side has already succeeded.

When the market moves between fills the answer is neither "abandon" nor
"chase": leg 2 is re-quoted up to the breakeven implied by leg 1's *actual*
fill, and leg 1 is unwound at market beyond it. That bounds the worst case at a
spread crossing plus fees instead of at a naked binary position.
"""

from __future__ import annotations

from decimal import Decimal

from arb.actions import Action, Alert, CancelOrder, PlaceOrder
from arb.events import (
    BalanceUpdate,
    BookUpdate,
    Fill,
    OrderAck,
    PartialFill,
    Reject,
    Timer,
    TunnelHealth,
)
from arb.reducer import step
from arb.state import State
from tests import builders as b


def orders(actions: tuple[Action, ...]) -> list[PlaceOrder]:
    return [a for a in actions if isinstance(a, PlaceOrder)]


def alerts(actions: tuple[Action, ...]) -> list[Alert]:
    return [a for a in actions if isinstance(a, Alert)]


def cancels(actions: tuple[Action, ...]) -> list[CancelOrder]:
    return [a for a in actions if isinstance(a, CancelOrder)]


def ready(
    *,
    kalshi_depth: int = 500,
    polymarket_depth: int = 500,
    kalshi_latency: int = 100,
    polymarket_latency: int = 100,
) -> tuple[State, tuple[Action, ...]]:
    """A state where one profitable candidate has just been accepted.

    Returns the state and the actions from the update that completed the pair,
    so a test can inspect the entry order directly.
    """
    matched = b.pair()
    state = b.state_with(matched)
    state, _ = step(state, BalanceUpdate("kalshi", Decimal("10000"), 1_000))
    state, _ = step(state, BalanceUpdate("polymarket", Decimal("10000"), 1_000))
    state, _ = step(
        state,
        TunnelHealth("kalshi", healthy=True, at_ms=1_000, latency_ms=kalshi_latency),
    )
    state, _ = step(
        state,
        TunnelHealth(
            "polymarket", healthy=True, at_ms=1_000, latency_ms=polymarket_latency
        ),
    )
    state, _ = step(
        state,
        BookUpdate(b.kalshi_book(matched, asks=(("0.05", kalshi_depth),))),
    )
    return step(
        state,
        BookUpdate(b.polymarket_book(matched, asks=(("0.90", polymarket_depth),))),
    )


def leg_one_filled(
    *, price: str = "0.05", size: int | None = None
) -> tuple[State, tuple[Action, ...], PlaceOrder]:
    """Drive to the point where leg 1 has filled and leg 2 has been placed."""
    state, actions = ready(kalshi_depth=100, polymarket_depth=500)
    first = orders(actions)[0]
    state, _ = step(state, OrderAck(first.order_id, 2_000))
    state, after = step(
        state,
        Fill(first.order_id, size or first.size, Decimal(price), 2_100),
    )
    return state, after, first


class TestLegOrdering:
    def test_only_one_order_is_placed_on_entry(self) -> None:
        """Sequential, not simultaneous: leg 2 does not exist until leg 1
        has actually filled."""
        _, actions = ready()

        assert len(orders(actions)) == 1

    def test_the_thinner_book_goes_first(self) -> None:
        _, actions = ready(kalshi_depth=60, polymarket_depth=5_000)

        assert orders(actions)[0].venue == "kalshi"

    def test_the_thinner_book_goes_first_whichever_venue_it_is(self) -> None:
        _, actions = ready(kalshi_depth=5_000, polymarket_depth=60)

        assert orders(actions)[0].venue == "polymarket"

    def test_the_slower_venue_goes_first_at_equal_depth(self) -> None:
        _, actions = ready(
            kalshi_depth=500,
            polymarket_depth=500,
            kalshi_latency=50,
            polymarket_latency=900,
        )

        assert orders(actions)[0].venue == "polymarket"

    def test_the_entry_order_is_a_taker_buy_at_the_walked_limit(self) -> None:
        _, actions = ready(kalshi_depth=100, polymarket_depth=500)
        first = orders(actions)[0]

        assert first.side == "buy"
        assert first.purpose == "leg1"
        assert first.size == 100
        assert first.limit_price == Decimal("0.05")

    def test_a_pair_already_being_legged_is_not_entered_twice(self) -> None:
        state, actions = ready()
        matched = b.pair()

        _, again = step(
            state,
            BookUpdate(b.polymarket_book(matched, asks=(("0.90", 500),))),
        )

        assert orders(again) == []


class TestLegTwo:
    def test_leg_two_is_placed_once_leg_one_fills(self) -> None:
        _, after, _ = leg_one_filled()
        second = orders(after)[0]

        assert second.venue == "polymarket"
        assert second.purpose == "leg2"
        assert second.side == "buy"
        assert second.size == 100

    def test_leg_two_is_bounded_by_the_breakeven_implied_by_leg_ones_fill(
        self,
    ) -> None:
        """A fill at the expected price still leaves the planned limit in
        force, because the planned limit is inside breakeven."""
        _, after, _ = leg_one_filled(price="0.05")

        assert orders(after)[0].limit_price == Decimal("0.90")

    def test_a_slightly_worse_leg_one_fill_leaves_the_planned_limit_standing(
        self,
    ) -> None:
        """Breakeven is a ceiling, not a target. A fill at 0.08 still leaves
        breakeven above the planned 0.90, so nothing tightens."""
        _, after, _ = leg_one_filled(price="0.08")

        assert orders(after)[0].limit_price == Decimal("0.90")

    def test_a_materially_worse_leg_one_fill_tightens_leg_twos_limit(self) -> None:
        """At 0.12 the breakeven falls below the planned limit, so leg 2 is
        capped at the price where the pair stops being profitable."""
        _, after, _ = leg_one_filled(price="0.12")

        assert orders(after)[0].limit_price < Decimal("0.90")

    def test_a_leg_one_fill_with_no_room_left_unwinds_instead_of_chasing(self) -> None:
        """Paying the full dollar for a binary leaves nothing for the other
        leg, and nothing is buyable at zero."""
        _, after, _ = leg_one_filled(price="1.00")
        placed = orders(after)

        assert [o.purpose for o in placed] == ["unwind"]
        assert placed[0].side == "sell"
        assert placed[0].venue == "kalshi"


class TestRequoting:
    def test_a_rejected_leg_two_is_requoted_up_to_breakeven(self) -> None:
        """User story 38: a moved market is salvaged rather than abandoned -
        but only as far as the breakeven implied by leg 1's actual fill."""
        state, after, _ = leg_one_filled()
        second = orders(after)[0]

        _, requote = step(state, Reject(second.order_id, "priced through", 2_200))

        placed = orders(requote)
        assert [o.purpose for o in placed] == ["leg2"]
        assert placed[0].limit_price > Decimal("0.90")
        # Breakeven against a 0.05 fill, not an arbitrary chase.
        assert placed[0].limit_price < Decimal("0.95")

    def test_a_second_rejection_unwinds_rather_than_chasing_further(self) -> None:
        state, after, _ = leg_one_filled()
        second = orders(after)[0]
        state, requote = step(state, Reject(second.order_id, "priced through", 2_200))
        requoted = orders(requote)[0]

        state, recovery = step(state, Reject(requoted.order_id, "still gone", 2_300))

        assert [o.purpose for o in orders(recovery)] == ["unwind"]
        assert state.leg_failures == 1

    def test_a_rejection_with_no_room_to_requote_unwinds_immediately(self) -> None:
        """Leg 1 filled at 0.12, so breakeven is already below the planned
        limit - there is nowhere to re-quote to."""
        state, after, _ = leg_one_filled(price="0.12")
        second = orders(after)[0]

        _, recovery = step(state, Reject(second.order_id, "no liquidity", 2_200))

        assert [o.purpose for o in orders(recovery)] == ["unwind"]


class TestPartialFills:
    def test_a_partial_leg_one_fill_sizes_leg_two_to_what_actually_filled(
        self,
    ) -> None:
        """User story 40: the pair stays balanced."""
        state, actions = ready(kalshi_depth=100, polymarket_depth=500)
        first = orders(actions)[0]

        _, after = step(state, PartialFill(first.order_id, 40, Decimal("0.05"), 2_100))

        second = orders(after)[0]
        assert second.purpose == "leg2"
        assert second.size == 40

    def test_a_partial_leg_two_fill_unwinds_only_the_unmatched_remainder(self) -> None:
        """User story 41: a mostly-successful trade is not discarded whole."""
        state, after, _ = leg_one_filled()
        second = orders(after)[0]

        state, recovery = step(
            state, PartialFill(second.order_id, 70, Decimal("0.90"), 2_200)
        )

        unwind = orders(recovery)
        assert [o.purpose for o in unwind] == ["unwind"]
        assert unwind[0].size == 30
        assert unwind[0].venue == "kalshi"

    def test_the_matched_portion_of_a_partial_pair_becomes_a_position(self) -> None:
        state, after, _ = leg_one_filled()
        second = orders(after)[0]

        state, _ = step(state, PartialFill(second.order_id, 70, Decimal("0.90"), 2_200))

        position = state.position_for(b.PAIR_ID)
        assert position is not None and position.size == 70


def unwinding() -> tuple[State, tuple[Action, ...]]:
    """Drive to the unwind.

    Leg 1 fills at 0.12, which puts breakeven below the planned limit, so the
    rejection has nowhere to re-quote to and goes straight to recovery.
    """
    state, after, _ = leg_one_filled(price="0.12")
    second = orders(after)[0]
    return step(state, Reject(second.order_id, "no liquidity", 2_200))


class TestTerminalEvents:
    """An order reports its outcome exactly once.

    A taker order that partially fills is finished - the remainder never
    happened - so `Fill`, `PartialFill` and `Reject` are all terminal. Venues do
    stream incremental fill messages, and aggregating those into one terminal
    event is the shell's job; the core refuses to guess which it is being sent.

    Without that, a second message for the same order is silently destructive:
    it overwrites the recorded fill size, places a second leg 2, and ends with
    two positions for one pair.
    """

    def test_a_second_terminal_event_for_one_order_is_ignored(self) -> None:
        state, actions = ready(kalshi_depth=100, polymarket_depth=500)
        first = orders(actions)[0]

        state, after = step(
            state, PartialFill(first.order_id, 40, Decimal("0.05"), 2_100)
        )
        state, duplicate = step(
            state, PartialFill(first.order_id, 60, Decimal("0.05"), 2_200)
        )

        assert [o.size for o in orders(after)] == [40]
        assert orders(duplicate) == []
        assert state.pending[b.PAIR_ID].first_filled_size == 40

    def test_a_duplicate_event_cannot_create_a_second_position(self) -> None:
        state, after, _ = leg_one_filled()
        second = orders(after)[0]

        state, _ = step(state, Fill(second.order_id, 100, Decimal("0.90"), 2_200))
        state, again = step(state, Fill(second.order_id, 100, Decimal("0.90"), 2_300))

        assert len(state.positions) == 1
        assert again == ()

    def test_a_fill_arriving_after_a_reject_is_ignored(self) -> None:
        state, after, _ = leg_one_filled(price="0.12")
        second = orders(after)[0]
        state, _ = step(state, Reject(second.order_id, "no liquidity", 2_200))

        state, late = step(state, Fill(second.order_id, 100, Decimal("0.90"), 2_300))

        assert late == ()
        assert state.position_for(b.PAIR_ID) is None


class TestLegFailure:
    def test_a_rejected_leg_two_unwinds_leg_one_at_market(self) -> None:
        """User story 39: the worst case is bounded at a spread crossing plus
        fees, not at a naked binary position."""
        _, recovery = unwinding()

        unwind = orders(recovery)[0]
        assert unwind.purpose == "unwind"
        assert unwind.side == "sell"
        assert unwind.venue == "kalshi"
        assert unwind.size == 100
        # Sold into the bid - `b.kalshi_book` defaults its bid to 0.09.
        assert unwind.limit_price == Decimal("0.09")

    def test_a_leg_failure_counts_against_the_budget(self) -> None:
        state, _ = unwinding()

        assert state.leg_failures == 1

    def test_a_leg_failure_alerts_the_operator(self) -> None:
        """User story 59: an overnight failure must not be discovered the
        following day."""
        _, recovery = unwinding()

        assert alerts(recovery)[0].severity == "critical"
        assert alerts(recovery)[0].pair_id == b.PAIR_ID

    def test_a_rejected_leg_one_is_not_a_leg_failure(self) -> None:
        """Nothing filled, so nothing is exposed and nothing needs unwinding.
        Counting it would exhaust the budget on non-events."""
        state, actions = ready()
        first = orders(actions)[0]

        state, recovery = step(state, Reject(first.order_id, "no liquidity", 2_000))

        assert state.leg_failures == 0
        assert orders(recovery) == []
        assert state.position_for(b.PAIR_ID) is None

    def test_the_unwind_cost_is_recorded_when_it_fills(self) -> None:
        """User story 43: Leg Failure has a measured cost rather than an
        assumed one."""
        state, recovery = unwinding()
        unwind = orders(recovery)[0]

        state, _ = step(state, Fill(unwind.order_id, 100, Decimal("0.09"), 2_300))

        # Bought 100 at 0.12, sold back into the 0.09 bid.
        assert state.unwind_cost == Decimal("3.00")

    def test_each_unwind_is_recorded_as_its_own_incident(self) -> None:
        """User story 43 asks for cost *per incident*. A running total cannot
        distinguish one expensive failure from ten cheap ones, and those call
        for different responses."""
        state, recovery = unwinding()
        unwind = orders(recovery)[0]

        state, _ = step(state, Fill(unwind.order_id, 100, Decimal("0.09"), 2_300))

        assert len(state.unwind_incidents) == 1
        incident = state.unwind_incidents[0]
        assert incident.pair_id == b.PAIR_ID
        assert incident.size == 100
        assert incident.entry_price == Decimal("0.12")
        assert incident.exit_price == Decimal("0.09")
        assert incident.cost == Decimal("3.00")
        assert incident.at_ms == 2_300


class TestCompletedEntry:
    def test_both_legs_filling_opens_a_position(self) -> None:
        state, after, _ = leg_one_filled()
        second = orders(after)[0]

        state, _ = step(state, Fill(second.order_id, 100, Decimal("0.90"), 2_200))

        position = state.position_for(b.PAIR_ID)
        assert position is not None
        assert position.size == 100
        assert position.kalshi_notional == Decimal("5.00")
        assert position.polymarket_notional == Decimal("90.00")

    def test_the_position_remembers_what_was_predicted(self) -> None:
        """So that realised profit can be reconciled against the model."""
        state, after, _ = leg_one_filled()
        second = orders(after)[0]

        state, _ = step(state, Fill(second.order_id, 100, Decimal("0.90"), 2_200))

        position = state.position_for(b.PAIR_ID)
        assert position is not None
        assert position.predicted_net_edge == Decimal("0.042175")

    def test_a_completed_entry_clears_the_pending_leg(self) -> None:
        state, after, _ = leg_one_filled()
        second = orders(after)[0]

        state, _ = step(state, Fill(second.order_id, 100, Decimal("0.90"), 2_200))

        assert state.pending == {}


class TestStuckEntries:
    """An entry the venue never answers must not strand the pair forever.

    Every other path out of the exposure window is driven by a venue message.
    If none arrives, nothing fires: the `PendingEntry` sits there holding budget
    and `_already_committed` blocks the pair from ever being entered again. The
    only thing that can break that is time, and time only reaches the reducer as
    a `Timer`.
    """

    def stuck(self, timeout_ms: int = 30_000) -> tuple[State, PlaceOrder]:
        matched = b.pair()
        state = b.state_with(matched, config_=b.config(entry_timeout_ms=timeout_ms))
        state, _ = step(
            state,
            BookUpdate(
                b.kalshi_book(matched, asks=(("0.05", 100),), received_time_ms=1_000)
            ),
        )
        state, actions = step(
            state,
            BookUpdate(
                b.polymarket_book(
                    matched, asks=(("0.90", 500),), received_time_ms=1_000
                )
            ),
        )
        first = orders(actions)[0]
        state, after = step(state, Fill(first.order_id, 100, Decimal("0.05"), 1_100))
        return state, orders(after)[0]

    def test_a_timer_inside_the_timeout_leaves_the_entry_alone(self) -> None:
        state, _ = self.stuck()

        state, actions = step(state, Timer(20_000))

        assert orders(actions) == []
        assert b.PAIR_ID in state.pending

    def test_a_timer_past_the_timeout_unwinds_the_stuck_leg(self) -> None:
        state, _ = self.stuck()

        state, actions = step(state, Timer(40_000))

        unwinds = [o for o in orders(actions) if o.purpose == "unwind"]
        assert [(o.venue, o.size) for o in unwinds] == [("kalshi", 100)]

    def test_the_unanswered_order_is_cancelled(self) -> None:
        state, outstanding = self.stuck()

        _, actions = step(state, Timer(40_000))

        assert [c.order_id for c in cancels(actions)] == [outstanding.order_id]

    def test_a_timeout_counts_as_a_leg_failure(self) -> None:
        """Unlike an abandoned entry, this *is* an execution failure - the venue
        did not answer - and a systematic one should exhaust the budget."""
        state, _ = self.stuck()

        state, _ = step(state, Timer(40_000))

        assert state.leg_failures == 1

    def test_an_entry_with_no_fill_yet_is_simply_dropped(self) -> None:
        matched = b.pair()
        state = b.state_with(matched, config_=b.config(entry_timeout_ms=30_000))
        state, _ = step(
            state,
            BookUpdate(
                b.kalshi_book(matched, asks=(("0.05", 100),), received_time_ms=1_000)
            ),
        )
        state, _ = step(
            state,
            BookUpdate(
                b.polymarket_book(
                    matched, asks=(("0.90", 500),), received_time_ms=1_000
                )
            ),
        )

        state, actions = step(state, Timer(40_000))

        assert [o.purpose for o in orders(actions)] == []
        assert b.PAIR_ID not in state.pending
        assert state.leg_failures == 0

    def test_timeouts_are_off_when_no_timeout_is_configured(self) -> None:
        state, _ = self.stuck(timeout_ms=0)

        state, actions = step(state, Timer(10_000_000))

        assert orders(actions) == []
        assert b.PAIR_ID in state.pending
