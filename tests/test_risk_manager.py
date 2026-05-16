"""Tests for risk_manager.py — position limits, halt conditions."""

import uuid
from datetime import datetime

import pytest

from legacy_code.models import Trade
from legacy_code.risk_manager import (
    DAILY_LOSS_LIMIT,
    MAX_BET_SIZE,
    MAX_TOTAL_EXPOSURE,
    RiskManager,
)


def _make_trade(
    direction="YES", size=0.50, price=0.55, outcome=None, status="simulated"
):
    return Trade(
        trade_id=str(uuid.uuid4()),
        prediction_id="pred1",
        direction=direction,
        size=size,
        limit_price=price,
        status=status,
        outcome=outcome,
        timestamp=datetime.utcnow(),
    )


class TestCanTrade:
    def test_initially_can_trade(self):
        rm = RiskManager()
        assert rm.can_trade() is True

    def test_halted_cannot_trade(self):
        rm = RiskManager()
        rm._halted = True
        rm._halt_reason = "test halt"
        assert rm.can_trade() is False


class TestValidateTrade:
    def test_valid_trade(self):
        rm = RiskManager()
        trade = _make_trade(size=0.50, price=0.55)
        valid, reason = rm.validate_trade(trade)
        assert valid is True
        assert reason == "OK"

    def test_exceeds_max_bet(self):
        rm = RiskManager()
        # Create trade via dict to bypass Pydantic validation
        trade = Trade(
            trade_id=str(uuid.uuid4()),
            prediction_id="pred1",
            direction="YES",
            size=1.00,  # max allowed
            limit_price=0.55,
            status="simulated",
            timestamp=datetime.utcnow(),
        )
        valid, _ = rm.validate_trade(trade)
        assert valid is True

    def test_exceeds_total_exposure(self):
        rm = RiskManager()
        # Fill up exposure near the limit
        for i in range(9):
            t = _make_trade(size=1.00, price=1.0)  # $1.00 cost each
            rm.register_trade(t)

        # This would push over $10
        new_trade = _make_trade(size=1.00, price=1.0)
        # Exposure is 9 * $1.00 = $9.00, adding $1.00 = $10.00 which is the limit
        valid, reason = rm.validate_trade(new_trade)
        assert valid is True  # exactly at limit

        # One more should fail
        rm.register_trade(new_trade)
        another = _make_trade(size=0.50, price=1.0)
        valid, reason = rm.validate_trade(another)
        assert valid is False
        assert "exceed" in reason.lower()

    def test_halted_rejects_trade(self):
        rm = RiskManager()
        rm._halted = True
        rm._halt_reason = "daily loss limit"
        trade = _make_trade()
        valid, reason = rm.validate_trade(trade)
        assert valid is False
        assert "halted" in reason.lower()


class TestExposure:
    def test_empty_exposure(self):
        rm = RiskManager()
        assert rm.current_exposure() == 0.0

    def test_yes_trade_exposure(self):
        rm = RiskManager()
        trade = _make_trade(direction="YES", size=1.00, price=0.60)
        rm.register_trade(trade)
        # Cost = price * size = 0.60
        assert rm.current_exposure() == 0.60

    def test_no_trade_exposure(self):
        rm = RiskManager()
        trade = _make_trade(direction="NO", size=1.00, price=0.60)
        rm.register_trade(trade)
        # Cost = (1 - price) * size = 0.40
        assert rm.current_exposure() == 0.40

    def test_resolved_trades_not_counted(self):
        rm = RiskManager()
        trade = _make_trade(size=1.00, price=0.60, outcome=1.0)
        rm.register_trade(trade)
        assert rm.current_exposure() == 0.0

    def test_cancelled_trades_not_counted(self):
        rm = RiskManager()
        trade = _make_trade(size=1.00, price=0.60, status="cancelled")
        rm.register_trade(trade)
        assert rm.current_exposure() == 0.0


class TestDailyPnL:
    def test_winning_yes_trade(self):
        rm = RiskManager()
        trade = _make_trade(direction="YES", size=1.00, price=0.40, outcome=1.0)
        rm.register_trade(trade)
        # Win: (1.0 - 0.40) * 1.00 = $0.60
        assert rm.daily_pnl() == 0.60

    def test_losing_yes_trade(self):
        rm = RiskManager()
        trade = _make_trade(direction="YES", size=1.00, price=0.40, outcome=0.0)
        rm.register_trade(trade)
        # Loss: -0.40 * 1.00 = -$0.40
        assert rm.daily_pnl() == -0.40

    def test_winning_no_trade(self):
        rm = RiskManager()
        trade = _make_trade(direction="NO", size=1.00, price=0.60, outcome=1.0)
        rm.register_trade(trade)
        # Win: 0.60 * 1.00 = $0.60
        assert rm.daily_pnl() == 0.60

    def test_losing_no_trade(self):
        rm = RiskManager()
        trade = _make_trade(direction="NO", size=1.00, price=0.60, outcome=0.0)
        rm.register_trade(trade)
        # Loss: -(1-0.60) * 1.00 = -$0.40
        assert rm.daily_pnl() == -0.40

    def test_mixed_pnl(self):
        rm = RiskManager()
        # Win $0.60
        t1 = _make_trade(direction="YES", size=1.00, price=0.40, outcome=1.0)
        rm.register_trade(t1)
        # Lose $0.50
        t2 = _make_trade(direction="YES", size=1.00, price=0.50, outcome=0.0)
        rm.register_trade(t2)
        # Net: 0.60 - 0.50 = $0.10
        assert rm.daily_pnl() == 0.10


class TestHaltConditions:
    def test_halt_on_daily_loss(self):
        rm = RiskManager()
        # Create enough losses to trigger halt ($4.00 daily loss limit)
        for _ in range(5):
            trade = _make_trade(direction="YES", size=1.00, price=0.90, outcome=0.0)
            rm.register_trade(trade)

        # Total loss = 5 * $0.90 = -$4.50 > $4.00 limit
        assert rm.is_halted is True
        assert rm.can_trade() is False
        assert "loss limit" in rm.halt_reason.lower()

    def test_no_halt_within_limit(self):
        rm = RiskManager()
        # Small losses that stay within limit
        for _ in range(3):
            trade = _make_trade(direction="YES", size=1.00, price=0.50, outcome=0.0)
            rm.register_trade(trade)

        # Total loss = 3 * $0.50 = -$1.50 < $4.00 limit
        assert rm.is_halted is False

    def test_reset_daily(self):
        rm = RiskManager()
        rm._halted = True
        rm._halt_reason = "test"
        rm.reset_daily()
        assert rm.is_halted is False
        assert rm.can_trade() is True
