"""Tests for edge_calculator.py — edge detection, Kelly criterion, bet sizing."""

from datetime import datetime, timedelta

import pytest

from legacy_code.edge_calculator import (
    MAX_BET_SIZE,
    MIN_EDGE_THRESHOLD,
    calculate_bet_size,
    calculate_edge,
    calculate_kelly,
    find_opportunities,
)
from legacy_code.models import Market, Prediction


def _make_market(market_id="m1", yes_price=0.50, volume=5000.0):
    return Market(
        market_id=market_id,
        question="Will X happen?",
        yes_price=yes_price,
        volume_24h=volume,
        end_date=datetime.utcnow() + timedelta(days=10),
        category="tech",
    )


def _make_prediction(market_id="m1", prob=0.70, market_price=0.50, confidence="medium"):
    return Prediction(
        market_id=market_id,
        claude_probability=prob,
        market_price=market_price,
        edge=round(prob - market_price, 4),
        confidence=confidence,
        reasoning="Test",
        bayesian_prior=0.5,
        news_quality_score=5,
    )


class TestEdgeCalculation:
    def test_positive_edge(self):
        assert calculate_edge(0.70, 0.50) == 0.20

    def test_negative_edge(self):
        assert calculate_edge(0.30, 0.50) == -0.20

    def test_zero_edge(self):
        assert calculate_edge(0.50, 0.50) == 0.0

    def test_extreme_edge(self):
        assert calculate_edge(1.0, 0.0) == 1.0

    def test_negative_extreme(self):
        assert calculate_edge(0.0, 1.0) == -1.0


class TestKellyCriterion:
    def test_favorable_bet(self):
        # 70% chance, even odds (odds=1.0): Kelly = (1*0.7-0.3)/1 = 0.4
        kelly = calculate_kelly(0.7, 1.0)
        assert kelly == pytest.approx(0.4, abs=0.01)

    def test_coin_flip_even_odds(self):
        # 50% chance, even odds: Kelly = 0
        kelly = calculate_kelly(0.5, 1.0)
        assert kelly == pytest.approx(0.0, abs=0.01)

    def test_unfavorable_bet(self):
        # 30% chance, even odds: Kelly would be negative → 0
        kelly = calculate_kelly(0.3, 1.0)
        assert kelly == 0.0

    def test_high_odds(self):
        # 60% chance, 3:1 odds: Kelly = (3*0.6-0.4)/3 = 1.4/3 = 0.467
        kelly = calculate_kelly(0.6, 3.0)
        assert kelly == pytest.approx(0.467, abs=0.01)

    def test_zero_probability(self):
        assert calculate_kelly(0.0, 1.0) == 0.0

    def test_zero_odds(self):
        assert calculate_kelly(0.7, 0.0) == 0.0

    def test_certainty(self):
        assert calculate_kelly(1.0, 1.0) == 0.0  # prob=1.0 edge case


class TestBetSize:
    def test_yes_bet_sizing(self):
        size = calculate_bet_size(0.70, 0.50, "YES")
        assert 0 < size <= MAX_BET_SIZE

    def test_no_bet_sizing(self):
        size = calculate_bet_size(0.30, 0.50, "NO")
        assert 0 < size <= MAX_BET_SIZE

    def test_max_cap(self):
        # Even with extreme edge, capped at $1.00
        size = calculate_bet_size(0.99, 0.01, "YES")
        assert size <= MAX_BET_SIZE

    def test_no_bet_when_no_edge(self):
        size = calculate_bet_size(0.50, 0.50, "YES")
        assert size == 0.0

    def test_boundary_prices(self):
        assert calculate_bet_size(0.5, 0.0, "YES") == 0.0
        assert calculate_bet_size(0.5, 1.0, "YES") == 0.0

    def test_two_decimal_places(self):
        size = calculate_bet_size(0.75, 0.50, "YES")
        assert size == round(size, 2)


class TestFindOpportunities:
    def test_finds_yes_opportunity(self):
        market = _make_market(yes_price=0.40)
        pred = _make_prediction(prob=0.70, market_price=0.40, confidence="high")
        opps = find_opportunities([market], [pred])
        assert len(opps) == 1
        assert opps[0].direction == "YES"
        assert opps[0].edge_pct >= MIN_EDGE_THRESHOLD * 100

    def test_finds_no_opportunity(self):
        market = _make_market(yes_price=0.70)
        pred = _make_prediction(prob=0.40, market_price=0.70, confidence="high")
        opps = find_opportunities([market], [pred])
        assert len(opps) == 1
        assert opps[0].direction == "NO"

    def test_skips_small_edge(self):
        market = _make_market(yes_price=0.50)
        pred = _make_prediction(prob=0.55, market_price=0.50, confidence="high")
        opps = find_opportunities([market], [pred])
        assert len(opps) == 0

    def test_skips_low_confidence(self):
        market = _make_market(yes_price=0.40)
        pred = _make_prediction(prob=0.70, market_price=0.40, confidence="low")
        opps = find_opportunities([market], [pred])
        assert len(opps) == 0

    def test_requires_both_edge_and_confidence(self):
        """Must have BOTH >15% edge AND medium/high confidence."""
        # High edge, low confidence
        market = _make_market(yes_price=0.30)
        pred = _make_prediction(prob=0.70, market_price=0.30, confidence="low")
        assert find_opportunities([market], [pred]) == []

        # Low edge, high confidence
        market2 = _make_market(yes_price=0.50)
        pred2 = _make_prediction(prob=0.55, market_price=0.50, confidence="high")
        assert find_opportunities([market2], [pred2]) == []

    def test_zero_edge_no_opportunity(self):
        market = _make_market(yes_price=0.50)
        pred = _make_prediction(prob=0.50, market_price=0.50, confidence="high")
        opps = find_opportunities([market], [pred])
        assert len(opps) == 0

    def test_multiple_markets(self):
        markets = [_make_market(f"m{i}", yes_price=0.3 + i * 0.1) for i in range(3)]
        preds = [
            _make_prediction(f"m0", prob=0.80, market_price=0.30, confidence="high"),  # 50% edge
            _make_prediction(f"m1", prob=0.45, market_price=0.40, confidence="high"),  # 5% edge - skip
            _make_prediction(f"m2", prob=0.90, market_price=0.50, confidence="medium"),  # 40% edge
        ]
        opps = find_opportunities(markets, preds)
        assert len(opps) == 2
