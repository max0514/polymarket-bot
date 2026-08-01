"""Settlement reconciliation and mismatch detection.

The strategy's entire premise is that both venues settle the same fact
identically. The one observation that can falsify it is what the two venues
actually paid - so each leg is recorded independently, and a pair whose legs
disagree is flagged loudly rather than absorbed into P&L as variance.

Mismatch is survivable if it is random: for a pair bought at `1 - e` with
mismatch rate `m` and `f` the fraction landing on the losing side, the expected
value is `e + m(1 - 2f)`. Directionally random mismatch adds variance but not
loss. Systematic mismatch is fatal. Telling those apart needs the labels this
module produces, which is why it exists from the start rather than being added
once someone wants the number.
"""

from __future__ import annotations

from decimal import Decimal

from arb.actions import Action, Alert, EmitSettlementRecord
from arb.events import Settlement
from arb.reducer import step
from arb.settlement import SettlementRecord
from arb.state import State
from tests import builders as b
from tests.test_kill_switch import holding
from tests.test_legging import alerts


def settlements(actions: tuple[Action, ...]) -> list[SettlementRecord]:
    return [a.record for a in actions if isinstance(a, EmitSettlementRecord)]


def settle(
    state: State,
    *,
    kalshi: str,
    polymarket: str,
    kalshi_at: int = 9_000,
    polymarket_at: int = 9_000,
) -> tuple[State, tuple[Action, ...]]:
    """Settle both legs, Kalshi first."""
    state, _ = step(
        state, Settlement(b.PAIR_ID, "kalshi", Decimal(kalshi), kalshi_at)
    )
    return step(
        state, Settlement(b.PAIR_ID, "polymarket", Decimal(polymarket), polymarket_at)
    )


class TestOneLegAtATime:
    def test_a_single_leg_settling_produces_no_record_yet(self) -> None:
        """Half a settlement is not a settlement."""
        state, actions = step(
            holding(), Settlement(b.PAIR_ID, "kalshi", Decimal("1"), 9_000)
        )

        assert settlements(actions) == []
        assert state.position_for(b.PAIR_ID) is not None

    def test_asymmetric_settlement_timing_is_recorded(self) -> None:
        """User story 60: the gap between the two venues is itself
        information."""
        _, actions = settle(
            holding(),
            kalshi="1",
            polymarket="0",
            kalshi_at=9_000,
            polymarket_at=95_000,
        )

        record = settlements(actions)[0]
        assert record.kalshi_settled_at_ms == 9_000
        assert record.polymarket_settled_at_ms == 95_000
        assert record.settlement_skew_ms == 86_000


class TestReconciliation:
    def test_a_clean_settlement_reconciles_realised_against_predicted(self) -> None:
        """User story 62: model error is measured continuously, not inferred."""
        _, actions = settle(holding(), kalshi="1", polymarket="0")
        record = settlements(actions)[0]

        # Bought 100 pairs at 0.05 + 0.90 = 95.00, one leg pays 100.00.
        # Fees at those prices: 0.07*0.05*0.95 + 0.05*0.90*0.10 = 0.007825/contract.
        assert record.cost == Decimal("95.00")
        assert record.fees_paid == Decimal("0.7825")
        assert record.realised_profit == Decimal("4.2175")
        assert record.predicted_profit == Decimal("4.2175")
        assert record.model_error == Decimal("0")

    def test_it_does_not_matter_which_leg_wins(self) -> None:
        _, actions = settle(holding(), kalshi="0", polymarket="1")

        assert settlements(actions)[0].realised_profit == Decimal("4.2175")

    def test_the_position_is_closed_once_both_legs_settle(self) -> None:
        state, _ = settle(holding(), kalshi="1", polymarket="0")

        assert state.position_for(b.PAIR_ID) is None
        assert state.positions == ()


class TestMismatch:
    def test_legs_that_both_lose_are_flagged(self) -> None:
        """A matched pair pays exactly one dollar per contract. Zero means the
        venues resolved the same fact differently."""
        _, actions = settle(holding(), kalshi="0", polymarket="0")
        record = settlements(actions)[0]

        assert record.mismatch is True
        assert record.realised_profit < 0

    def test_legs_that_both_win_are_flagged(self) -> None:
        _, actions = settle(holding(), kalshi="1", polymarket="1")

        assert settlements(actions)[0].mismatch is True

    def test_a_mismatch_alerts_loudly_rather_than_being_absorbed(self) -> None:
        """User story 61: a matching failure is never absorbed silently into
        P&L."""
        _, actions = settle(holding(), kalshi="0", polymarket="0")

        assert alerts(actions)[0].severity == "critical"
        assert alerts(actions)[0].pair_id == b.PAIR_ID

    def test_a_clean_settlement_does_not_alert(self) -> None:
        _, actions = settle(holding(), kalshi="1", polymarket="0")

        assert alerts(actions) == []

    def test_a_profitable_mismatch_is_still_a_mismatch(self) -> None:
        """Both legs winning is good for this trade and terrible news about the
        pair. Judging it on P&L would file it as a success."""
        _, actions = settle(holding(), kalshi="1", polymarket="1")
        record = settlements(actions)[0]

        assert record.realised_profit > 0
        assert record.mismatch is True


class TestGroundTruthForCalibration:
    def test_the_record_carries_the_label_the_registry_needs(self) -> None:
        """User story 16: post-settlement confirmation that both venues paid
        identically, which is the calibration dataset's only real label."""
        _, clean = settle(holding(), kalshi="1", polymarket="0")
        _, broken = settle(holding(), kalshi="0", polymarket="0")

        assert settlements(clean)[0].as_record()["settled_identically"] == "1"
        assert settlements(broken)[0].as_record()["settled_identically"] == "0"

    def test_the_record_is_serialisable_for_analysis(self) -> None:
        _, actions = settle(holding(), kalshi="1", polymarket="0")
        row = settlements(actions)[0].as_record()

        assert row["pair_id"] == b.PAIR_ID
        assert row["size"] == "100"
        assert row["realised_profit"] == "4.21750000"
        assert row["predicted_profit"] == "4.21750000"
        assert row["model_error"] == "0.00000000"
        assert row["mismatch"] == "0"
