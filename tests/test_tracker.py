"""Tests for tracker.py — Brier Score calculation and DB operations."""

import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from legacy_code.models import Prediction, Trade
from legacy_code.tracker import Tracker


@pytest.fixture
def tracker(tmp_path):
    db_path = tmp_path / "test.db"
    return Tracker(db_path=db_path)


def _make_prediction(market_id="m1", prob=0.7, market_price=0.5, confidence="medium"):
    return Prediction(
        prediction_id=str(uuid.uuid4()),
        market_id=market_id,
        timestamp=datetime.utcnow(),
        claude_probability=prob,
        market_price=market_price,
        edge=round(prob - market_price, 4),
        confidence=confidence,
        reasoning="Test reasoning",
        bayesian_prior=0.5,
        key_evidence=["fact1", "fact2"],
        risks=["risk1"],
        news_articles=["http://example.com"],
        news_quality_score=3,
    )


def _make_trade(prediction_id, direction="YES", size=0.50, price=0.55, outcome=None):
    return Trade(
        trade_id=str(uuid.uuid4()),
        prediction_id=prediction_id,
        direction=direction,
        size=size,
        limit_price=price,
        status="simulated" if outcome is None else "filled",
        outcome=outcome,
        timestamp=datetime.utcnow(),
    )


class TestBrierScore:
    def test_perfect_prediction(self, tracker):
        """Brier score = 0 when predictions are perfect."""
        pred = _make_prediction(prob=1.0)
        tracker.save_prediction(pred)
        trade = _make_trade(pred.prediction_id, direction="YES", outcome=1.0)
        tracker.save_trade(trade)

        bs = tracker.calculate_brier_score()
        assert bs == 0.0

    def test_worst_prediction(self, tracker):
        """Brier score = 1 when predictions are maximally wrong."""
        pred = _make_prediction(prob=1.0)
        tracker.save_prediction(pred)
        trade = _make_trade(pred.prediction_id, direction="YES", outcome=0.0)
        tracker.save_trade(trade)

        bs = tracker.calculate_brier_score()
        assert bs == 1.0

    def test_coin_flip(self, tracker):
        """Brier score = 0.25 for 50/50 prediction."""
        pred = _make_prediction(prob=0.5)
        tracker.save_prediction(pred)
        trade = _make_trade(pred.prediction_id, direction="YES", outcome=1.0)
        tracker.save_trade(trade)

        bs = tracker.calculate_brier_score()
        assert bs == 0.25

    def test_brier_score_multiple_predictions(self, tracker):
        """Brier score averages across multiple predictions."""
        # Perfect prediction
        p1 = _make_prediction(market_id="m1", prob=0.9)
        tracker.save_prediction(p1)
        t1 = _make_trade(p1.prediction_id, direction="YES", outcome=1.0)
        tracker.save_trade(t1)

        # Bad prediction
        p2 = _make_prediction(market_id="m2", prob=0.9)
        tracker.save_prediction(p2)
        t2 = _make_trade(p2.prediction_id, direction="YES", outcome=0.0)
        tracker.save_trade(t2)

        bs = tracker.calculate_brier_score()
        # (0.9-1)^2 + (0.9-0)^2 = 0.01 + 0.81 = 0.82, avg = 0.41
        assert bs == pytest.approx(0.41, abs=0.01)

    def test_no_resolved_predictions(self, tracker):
        """Brier score is None when no predictions resolved."""
        pred = _make_prediction()
        tracker.save_prediction(pred)
        trade = _make_trade(pred.prediction_id, outcome=None)
        tracker.save_trade(trade)

        bs = tracker.calculate_brier_score()
        assert bs is None

    def test_empty_db(self, tracker):
        """Brier score is None on empty database."""
        bs = tracker.calculate_brier_score()
        assert bs is None

    def test_no_direction_bias(self, tracker):
        """NO direction trades use 1-prob for Brier score."""
        pred = _make_prediction(prob=0.2)  # Claude thinks YES is 20%, NO is 80%
        tracker.save_prediction(pred)
        trade = _make_trade(pred.prediction_id, direction="NO", outcome=1.0)
        tracker.save_trade(trade)

        bs = tracker.calculate_brier_score()
        # NO prob = 1-0.2 = 0.8, outcome=1.0, (0.8-1.0)^2 = 0.04
        assert bs == pytest.approx(0.04, abs=0.001)

    def test_target_brier_score(self, tracker):
        """A well-calibrated bot should achieve Brier < 0.20."""
        # Simulate good calibration
        test_cases = [
            (0.8, 1.0, "YES"),
            (0.7, 1.0, "YES"),
            (0.9, 1.0, "YES"),
            (0.3, 0.0, "YES"),
            (0.2, 0.0, "YES"),
        ]
        for prob, outcome, direction in test_cases:
            p = _make_prediction(market_id=f"m{prob}", prob=prob)
            tracker.save_prediction(p)
            t = _make_trade(p.prediction_id, direction=direction, outcome=outcome)
            tracker.save_trade(t)

        bs = tracker.calculate_brier_score()
        assert bs is not None
        assert bs < 0.20, f"Brier score {bs} exceeds target 0.20"


class TestTrackerDB:
    def test_save_and_retrieve_prediction(self, tracker):
        pred = _make_prediction()
        pred_id = tracker.save_prediction(pred)

        all_preds = tracker.get_all_predictions()
        assert len(all_preds) == 1
        assert all_preds[0]["prediction_id"] == pred_id

    def test_save_and_retrieve_trade(self, tracker):
        pred = _make_prediction()
        tracker.save_prediction(pred)
        trade = _make_trade(pred.prediction_id)
        trade_id = tracker.save_trade(trade)

        all_trades = tracker.get_all_trades()
        assert len(all_trades) == 1
        assert all_trades[0]["trade_id"] == trade_id

    def test_update_trade_outcome(self, tracker):
        pred = _make_prediction()
        tracker.save_prediction(pred)
        trade = _make_trade(pred.prediction_id)
        trade_id = tracker.save_trade(trade)

        tracker.update_trade_outcome(trade_id, 1.0)
        resolved = tracker.get_resolved_predictions()
        assert len(resolved) == 1

    def test_generate_report(self, tracker):
        pred = _make_prediction()
        tracker.save_prediction(pred)
        trade = _make_trade(pred.prediction_id, outcome=1.0)
        tracker.save_trade(trade)

        report = tracker.generate_report()
        assert "brier_score" in report
        assert "total_predictions" in report
        assert report["total_predictions"] == 1
        assert report["total_trades"] == 1
        assert report["resolved_trades"] == 1


class TestCalibration:
    def test_calibration_buckets(self, tracker):
        """Verify calibration grouping works."""
        test_cases = [
            (0.75, 1.0),
            (0.72, 1.0),
            (0.78, 0.0),
            (0.25, 0.0),
            (0.22, 0.0),
        ]
        for prob, outcome in test_cases:
            p = _make_prediction(market_id=f"m{prob}", prob=prob)
            tracker.save_prediction(p)
            t = _make_trade(p.prediction_id, direction="YES", outcome=outcome)
            tracker.save_trade(t)

        cal = tracker.calculate_calibration()
        assert len(cal) > 0
        # 70-80% bucket should have 3 entries
        bucket_70 = cal.get("70%-80%", {})
        assert bucket_70.get("count") == 3

    def test_empty_calibration(self, tracker):
        cal = tracker.calculate_calibration()
        assert cal == {}
