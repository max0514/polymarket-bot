"""Edge calculation and Kelly Criterion bet sizing."""

import logging
import math

from .models import Market, Prediction, TradeOpportunity

logger = logging.getLogger(__name__)

MIN_EDGE_THRESHOLD = 0.15  # 15% minimum edge
KELLY_FRACTION = 0.25  # Use 25% of full Kelly
MAX_BET_SIZE = 1.00  # $1.00 cap per bet
REQUIRED_CONFIDENCE = ("medium", "high")


def calculate_edge(claude_prob: float, market_price: float) -> float:
    """Calculate edge = claude_probability - market_price."""
    return round(claude_prob - market_price, 4)


def calculate_kelly(probability: float, odds: float) -> float:
    """Calculate Kelly Criterion bet fraction.

    Kelly % = (bp - q) / b
    where b = odds, p = probability of winning, q = 1 - p

    For binary markets, odds = (1 / market_price) - 1 for YES bets.
    """
    if odds <= 0 or probability <= 0 or probability >= 1:
        return 0.0

    q = 1.0 - probability
    kelly = (odds * probability - q) / odds

    return max(0.0, kelly)


def calculate_bet_size(
    probability: float, market_price: float, direction: str
) -> float:
    """Calculate fractional Kelly bet size, capped at MAX_BET_SIZE.

    Args:
        probability: Claude's estimated probability (YES side).
        market_price: Current market YES price.
        direction: "YES" or "NO".
    """
    if direction == "YES":
        if market_price <= 0 or market_price >= 1:
            return 0.0
        odds = (1.0 / market_price) - 1.0
        full_kelly = calculate_kelly(probability, odds)
    else:
        no_price = 1.0 - market_price
        if no_price <= 0 or no_price >= 1:
            return 0.0
        no_prob = 1.0 - probability
        odds = (1.0 / no_price) - 1.0
        full_kelly = calculate_kelly(no_prob, odds)

    fractional = full_kelly * KELLY_FRACTION
    size = min(fractional, MAX_BET_SIZE)
    return round(size, 2)


def find_opportunities(
    markets: list[Market], predictions: list[Prediction]
) -> list[TradeOpportunity]:
    """Find trading opportunities where edge and confidence are sufficient."""
    opportunities = []

    for market, prediction in zip(markets, predictions):
        edge = calculate_edge(prediction.claude_probability, market.yes_price)

        # Determine direction
        if edge > 0:
            direction = "YES"
        elif edge < 0:
            direction = "NO"
        else:
            continue

        abs_edge = abs(edge)

        # Require both sufficient edge AND confidence
        if abs_edge < MIN_EDGE_THRESHOLD:
            logger.debug(
                "Skipping %s: edge %.2f%% below threshold", market.market_id, abs_edge * 100
            )
            continue

        if prediction.confidence not in REQUIRED_CONFIDENCE:
            logger.debug(
                "Skipping %s: confidence '%s' too low", market.market_id, prediction.confidence
            )
            continue

        # News quality warning
        if prediction.news_quality_score is not None and prediction.news_quality_score < 3:
            logger.warning(
                "Low news quality for %s: only %d articles found",
                market.market_id,
                prediction.news_quality_score,
            )

        suggested_size = calculate_bet_size(
            prediction.claude_probability, market.yes_price, direction
        )

        if suggested_size <= 0:
            continue

        kelly_frac = suggested_size / MAX_BET_SIZE

        opp = TradeOpportunity(
            market=market,
            prediction=prediction,
            direction=direction,
            suggested_size=suggested_size,
            edge_pct=round(abs_edge * 100, 2),
            confidence=prediction.confidence,
            kelly_fraction=round(kelly_frac, 4),
        )
        opportunities.append(opp)
        logger.info(
            "Found opportunity: %s %s edge=%.1f%% size=$%.2f",
            market.market_id,
            direction,
            abs_edge * 100,
            suggested_size,
        )

    return opportunities
