"""The tiered kill switch and event-driven early exit.

The governing asymmetry: an open pair is the *safest* asset in this book. Its
exposure decays to zero at settlement, and closing it early pays a second round
of taker fees against a profit that is already locked. So the default kill
action stops new entries and holds what is open, and flattening is reserved for
account emergencies rather than used as a reflex.

Early exit is therefore an emergency valve, not a strategy. It fires on
discrete events that mean a pair has stopped being riskless - never on price
movement, which for a held pair is simply irrelevant.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from arb.actions import Action, Alert, CancelOrder, EmitExitRecord, PlaceOrder
from arb.decisions import RejectionReason
from arb.events import (
    BookUpdate,
    Fill,
    Reject,
    DisputeOpened,
    KillSwitch,
    Postponement,
    RuleDivergenceFound,
)
from arb.exits import ExitRecord
from arb.reducer import step
from arb.risk import UNLIMITED
from arb.state import KillTier, State
from tests import builders as b
from tests.builders import records
from tests.test_legging import alerts, leg_one_filled, orders, ready
from tests.test_risk import entry_attempt, healthy_state


def holding() -> State:
    """A state with one open position on the default pair."""
    from arb.events import Fill

    state, after, _ = leg_one_filled()
    second = orders(after)[0]
    state, _ = step(state, Fill(second.order_id, 100, Decimal("0.90"), 2_200))
    return state


def exits(actions: tuple[Action, ...]) -> list[PlaceOrder]:
    return [o for o in orders(actions) if o.purpose == "exit"]


def exit_records(actions: tuple[Action, ...]) -> list[ExitRecord]:
    return [a.record for a in actions if isinstance(a, EmitExitRecord)]


def cancels(actions: tuple[Action, ...]) -> list[CancelOrder]:
    return [a for a in actions if isinstance(a, CancelOrder)]


class TestTierOne:
    def test_stopping_entries_blocks_new_candidates(self) -> None:
        state, _ = step(healthy_state(), KillSwitch("stop_entries", 5_000))

        _, emitted = entry_attempt(state)

        assert emitted[0].accepted is False
        assert emitted[0].rejection_reason is RejectionReason.KILL_SWITCH

    def test_stopping_entries_does_not_touch_open_positions(self) -> None:
        """The safety system must not destroy locked profit."""
        state, actions = step(holding(), KillSwitch("stop_entries", 5_000))

        assert exits(actions) == []
        assert state.position_for(b.PAIR_ID) is not None


class TestTierTwo:
    def test_exiting_flagged_positions_leaves_unflagged_ones_alone(self) -> None:
        """One bad market must not force a whole-book liquidation."""
        state, actions = step(holding(), KillSwitch("exit_flagged", 5_000))

        assert exits(actions) == []
        assert state.position_for(b.PAIR_ID) is not None

    def test_a_position_flagged_but_not_yet_exiting_is_swept_by_tier_two(self) -> None:
        """The tier is the manual sweep for anything flagged whose automatic
        exit did not take - it is not a second copy of the trigger."""
        state = holding()
        flagged = replace(
            state.positions[0], exit_trigger="rule_divergence", exiting=False
        )
        state = replace(state, positions=(flagged,))

        state, actions = step(state, KillSwitch("exit_flagged", 6_000))

        assert len(exits(actions)) == 2
        position = state.position_for(b.PAIR_ID)
        assert position is not None and position.exiting is True


class TestTierThree:
    def test_flattening_exits_every_position(self) -> None:
        state, actions = step(holding(), KillSwitch("flatten_all", 5_000))

        assert {o.venue for o in exits(actions)} == {"kalshi", "polymarket"}
        assert state.kill_tier is KillTier.FLATTEN_ALL

    def test_flattening_sells_both_legs_into_the_bid(self) -> None:
        _, actions = step(holding(), KillSwitch("flatten_all", 5_000))

        for order in exits(actions):
            assert order.side == "sell"
            assert order.size == 100
        # Builder bids: kalshi 0.09, polymarket 0.87.
        assert {o.limit_price for o in exits(actions)} == {
            Decimal("0.09"),
            Decimal("0.87"),
        }

    def test_flattening_twice_does_not_double_sell(self) -> None:
        state, _ = step(holding(), KillSwitch("flatten_all", 5_000))
        _, again = step(state, KillSwitch("flatten_all", 6_000))

        assert exits(again) == []


class TestEarlyExitTriggers:
    def test_a_dispute_on_the_polymarket_leg_exits_the_pair(self) -> None:
        """User story 56: a position that has stopped being riskless is
        closed."""
        state, actions = step(holding(), DisputeOpened(b.PAIR_ID, 5_000))

        assert len(exits(actions)) == 2
        position = state.position_for(b.PAIR_ID)
        assert position is not None and position.exit_trigger == "dispute_opened"

    def test_rule_divergence_found_after_entry_exits_the_pair(self) -> None:
        """User story 57: a matching error is corrected rather than held."""
        state, actions = step(
            holding(),
            RuleDivergenceFound(b.PAIR_ID, "overtime rule differs", 5_000),
        )

        assert len(exits(actions)) == 2
        position = state.position_for(b.PAIR_ID)
        assert position is not None
        assert position.exit_trigger == "rule_divergence"

    def test_postponement_exits_the_pair(self) -> None:
        """User story 58: void asymmetry is escaped before it settles."""
        state, actions = step(holding(), Postponement(b.PAIR_ID, 5_000))

        assert len(exits(actions)) == 2
        position = state.position_for(b.PAIR_ID)
        assert position is not None and position.exit_trigger == "postponement"

    def test_every_trigger_alerts_the_operator(self) -> None:
        """User story 59: overnight events are not discovered the following
        day."""
        _, actions = step(holding(), DisputeOpened(b.PAIR_ID, 5_000))

        assert alerts(actions)[0].severity == "critical"
        assert alerts(actions)[0].pair_id == b.PAIR_ID

    def test_a_trigger_for_a_pair_we_do_not_hold_does_nothing(self) -> None:
        _, actions = step(holding(), DisputeOpened("some-other-pair", 5_000))

        assert exits(actions) == []

    def test_a_price_move_alone_does_not_trigger_an_exit(self) -> None:
        """The spec's coverage item. Post-entry price movement is irrelevant to
        a locked pair - exiting to bank it costs a second round of taker fees
        against a profit already earned."""
        state = holding()
        matched = b.pair()

        state, actions = step(
            state,
            BookUpdate(b.kalshi_book(matched, asks=(("0.60", 500),), bids=(("0.59", 500),))),
        )

        assert exits(actions) == []
        assert records(actions) == []
        assert state.position_for(b.PAIR_ID) is not None


class TestExitFills:
    """An exit that fills has to close the position in the core too.

    Selling at the venue and still holding the position in `State` is the worst
    of both: the capital is gone but its budget is still consumed, the pair can
    never be re-entered, and a later settlement credits a payout the operator no
    longer owns.
    """

    def flattening(self) -> tuple[State, list[PlaceOrder]]:
        state, actions = step(holding(), KillSwitch("flatten_all", 5_000))
        return state, exits(actions)

    def test_both_exit_legs_filling_closes_the_position(self) -> None:
        state, sells = self.flattening()

        for order in sells:
            state, _ = step(state, Fill(order.order_id, 100, order.limit_price, 6_000))

        assert state.position_for(b.PAIR_ID) is None
        assert state.positions == ()

    def test_one_exit_leg_filling_does_not_close_the_position(self) -> None:
        """Half an exit is still a live pair - and now an unbalanced one."""
        state, sells = self.flattening()

        state, _ = step(state, Fill(sells[0].order_id, 100, sells[0].limit_price, 6_000))

        assert state.position_for(b.PAIR_ID) is not None

    def test_closing_releases_the_capital_the_position_was_holding(self) -> None:
        state, sells = self.flattening()
        assert state.risk_budgets.unsettled_capital_remaining < UNLIMITED

        for order in sells:
            state, _ = step(state, Fill(order.order_id, 100, order.limit_price, 6_000))

        assert state.risk_budgets.unsettled_capital_remaining == UNLIMITED

    def test_a_rejected_exit_leg_says_so_loudly(self) -> None:
        """A refused exit leg leaves the other one naked. That is the single
        most dangerous state this system can be in, so it is never silent."""
        state, sells = self.flattening()
        state, _ = step(state, Fill(sells[0].order_id, 100, sells[0].limit_price, 6_000))

        _, actions = step(state, Reject(sells[1].order_id, "no bid", 6_100))

        assert any("naked" in alert.message for alert in alerts(actions))
        assert alerts(actions)[0].severity == "critical"

    def test_the_result_of_an_early_exit_is_recorded(self) -> None:
        """An early exit is a realised loss against a locked profit. Dropping
        it would leave the decision log flattering the strategy."""
        state, sells = self.flattening()

        for order in sells:
            state, actions = step(
                state, Fill(order.order_id, 100, order.limit_price, 6_000)
            )

        record = exit_records(actions)[0]
        assert record.pair_id == b.PAIR_ID
        assert record.size == 100
        assert record.trigger == "flatten_all"
        # Entered at 0.05 + 0.90 = 95.00, sold into bids 0.09 + 0.87 = 96.00.
        assert record.cost == Decimal("95.00")
        assert record.proceeds == Decimal("96.00")


class TestTriggersDuringTheExposureWindow:
    """A pair mid-entry is the most dangerous place for a trigger to arrive.

    Between leg 1 and leg 2 the system holds an unhedged binary. A dispute or
    postponement landing in that window means the pair has stopped being worth
    completing - so the entry is abandoned, not finished. Dropping the trigger
    because no `Position` exists yet completes the entry into a market that is
    already known to be broken.
    """

    def test_a_dispute_after_leg_one_filled_unwinds_instead_of_completing(self) -> None:
        state, after, _ = leg_one_filled()
        assert orders(after)[0].purpose == "leg2"

        state, actions = step(state, DisputeOpened(b.PAIR_ID, 5_000))

        unwinds = [o for o in orders(actions) if o.purpose == "unwind"]
        assert [(o.venue, o.size) for o in unwinds] == [("kalshi", 100)]
        assert state.position_for(b.PAIR_ID) is None

    def test_the_pair_is_released_only_once_the_unwind_reports(self) -> None:
        """The entry stays pending while the unwind is in flight - the pair is
        not finished with, and must not be re-entered underneath itself."""
        state, _, _ = leg_one_filled()
        state, actions = step(state, DisputeOpened(b.PAIR_ID, 5_000))
        unwind = [o for o in orders(actions) if o.purpose == "unwind"][0]
        assert b.PAIR_ID in state.pending

        state, _ = step(state, Fill(unwind.order_id, 100, Decimal("0.09"), 6_000))

        assert b.PAIR_ID not in state.pending
        # Bought at 0.05, sold into the 0.09 bid: this unwind made money, so
        # the recorded cost is negative. Leg Failures are usually expensive,
        # not always.
        assert state.unwind_cost == Decimal("-4.00")

    def test_the_outstanding_leg_two_order_is_cancelled(self) -> None:
        """Leaving it live would let the venue fill it into a pair the system
        has just decided to abandon."""
        state, after, _ = leg_one_filled()
        outstanding = orders(after)[0]

        _, actions = step(state, DisputeOpened(b.PAIR_ID, 5_000))

        assert [c.order_id for c in cancels(actions)] == [outstanding.order_id]

    def test_a_trigger_before_any_fill_abandons_without_unwinding(self) -> None:
        """Nothing filled, so there is nothing to sell back - just stop."""
        state, entry = ready(kalshi_depth=100, polymarket_depth=500)
        leg_one = orders(entry)[0]

        state, actions = step(state, Postponement(b.PAIR_ID, 5_000))

        assert [o.purpose for o in orders(actions)] == []
        assert [c.order_id for c in cancels(actions)] == [leg_one.order_id]
        assert b.PAIR_ID not in state.pending

    def test_the_operator_is_alerted(self) -> None:
        state, _, _ = leg_one_filled()

        _, actions = step(state, DisputeOpened(b.PAIR_ID, 5_000))

        assert alerts(actions)[0].severity == "critical"
        assert alerts(actions)[0].pair_id == b.PAIR_ID

    def test_a_cancelled_entry_does_not_count_against_the_leg_failure_budget(
        self,
    ) -> None:
        """The execution path did not fail - the market did. Charging it to the
        Leg Failure budget would halt the system for someone else's problem."""
        state, entry = ready(kalshi_depth=100, polymarket_depth=500)

        state, _ = step(state, Postponement(b.PAIR_ID, 5_000))

        assert state.leg_failures == 0
