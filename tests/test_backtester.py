"""Tests for backtester.py — outcome parsing, backtest report formatting."""

import json
from datetime import datetime, timezone

import pytest

from legacy_code.backtester import (
    _parse_close_price,
    _parse_outcome,
    format_backtest_report,
)


class TestParseOutcome:
    def test_yes_string(self):
        assert _parse_outcome({"resolution": "Yes"}) == 1.0

    def test_no_string(self):
        assert _parse_outcome({"resolution": "No"}) == 0.0

    def test_true_string(self):
        assert _parse_outcome({"resolution": "true"}) == 1.0

    def test_false_string(self):
        assert _parse_outcome({"resolution": "false"}) == 0.0

    def test_numeric_1(self):
        assert _parse_outcome({"resolution": "1"}) == 1.0

    def test_numeric_0(self):
        assert _parse_outcome({"resolution": "0"}) == 0.0

    def test_outcome_prices_yes(self):
        market = {"outcomePrices": json.dumps([0.95, 0.05])}
        assert _parse_outcome(market) == 1.0

    def test_outcome_prices_no(self):
        market = {"outcomePrices": json.dumps([0.05, 0.95])}
        assert _parse_outcome(market) == 0.0

    def test_outcome_prices_list(self):
        market = {"outcomePrices": [0.95, 0.05]}
        assert _parse_outcome(market) == 1.0

    def test_unresolved_returns_none(self):
        assert _parse_outcome({}) is None

    def test_ambiguous_prices_returns_none(self):
        """Prices near 50/50 are not clearly resolved."""
        market = {"outcomePrices": json.dumps([0.5, 0.5])}
        assert _parse_outcome(market) is None

    def test_winner_field_yes(self):
        assert _parse_outcome({"winner": "Yes"}) == 1.0

    def test_winner_field_no(self):
        assert _parse_outcome({"winner": "No"}) == 0.0


class TestParseClosePrice:
    def test_from_outcome_prices_string(self):
        market = {"outcomePrices": json.dumps([0.75, 0.25])}
        assert _parse_close_price(market) == 0.75

    def test_from_tokens(self):
        market = {"tokens": [{"outcome": "YES", "price": 0.60}]}
        assert _parse_close_price(market) == 0.60

    def test_from_bid_ask(self):
        market = {"bestBid": 0.55, "bestAsk": 0.65}
        assert _parse_close_price(market) == pytest.approx(0.60)

    def test_fallback_to_half(self):
        assert _parse_close_price({}) == 0.5


class TestFormatReport:
    def test_error_report(self):
        report = format_backtest_report({"error": "No markets"})
        assert "failed" in report.lower()

    def test_full_report_format(self):
        results = {
            "markets_tested": 5,
            "predictions_made": 5,
            "trades_simulated": 3,
            "brier_score_mean": 0.18,
            "direction_accuracy": 0.80,
            "calibration": {
                "60%-70%": {"actual_rate": 0.65, "count": 2},
                "80%-90%": {"actual_rate": 0.85, "count": 3},
            },
            "market_details": [
                {
                    "question": "Will event X happen?",
                    "outcome": "YES",
                    "claude_prob": 0.75,
                    "market_price": 0.50,
                    "brier": 0.0625,
                    "edge": 0.25,
                    "confidence": "high",
                    "news_count": 5,
                    "correct": True,
                },
            ],
        }
        report = format_backtest_report(results)
        assert "BACKTEST REPORT" in report
        assert "5" in report  # markets tested
        assert "0.18" in report  # brier score
        assert "80.0%" in report  # direction accuracy
        assert "Will event X happen?" in report

    def test_report_with_no_calibration(self):
        results = {
            "markets_tested": 1,
            "predictions_made": 1,
            "trades_simulated": 0,
            "brier_score_mean": 0.25,
            "direction_accuracy": 0.50,
            "calibration": {},
            "market_details": [],
        }
        report = format_backtest_report(results)
        assert "BACKTEST REPORT" in report

    def test_correct_incorrect_markers(self):
        results = {
            "markets_tested": 2,
            "predictions_made": 2,
            "trades_simulated": 0,
            "brier_score_mean": 0.3,
            "direction_accuracy": 0.5,
            "calibration": {},
            "market_details": [
                {
                    "question": "Correct prediction",
                    "outcome": "YES",
                    "claude_prob": 0.8,
                    "market_price": 0.5,
                    "brier": 0.04,
                    "edge": 0.3,
                    "confidence": "high",
                    "news_count": 5,
                    "correct": True,
                },
                {
                    "question": "Wrong prediction",
                    "outcome": "NO",
                    "claude_prob": 0.8,
                    "market_price": 0.5,
                    "brier": 0.64,
                    "edge": 0.3,
                    "confidence": "high",
                    "news_count": 3,
                    "correct": False,
                },
            ],
        }
        report = format_backtest_report(results)
        assert "✓" in report
        assert "✗" in report
