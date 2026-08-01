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

from arb.actions import Action, Alert, PlaceOrder
from arb.decisions import RejectionReason
from arb.events import (
    BookUpdate,
    DisputeOpened,
    KillSwitch,
    Postponement,
    RuleDivergenceFound,
)
from arb.reducer import step
from arb.state import KillTier, State
from tests import builders as b
from tests.builders import records
from tests.test_legging import alerts, leg_one_filled, orders
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
