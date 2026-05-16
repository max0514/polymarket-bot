"""Backtesting engine for the Polymarket trading bot.

Fetches resolved markets from Polymarket, replays the full pipeline
(news fetch → Claude estimation → edge calculation → simulated trades),
and compares predictions against actual outcomes.

Key design: temporal correctness — only uses news published BEFORE
the prediction date to prevent look-ahead bias.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from .edge_calculator import find_opportunities
from .models import Market, Prediction, Trade
from .news_fetcher import fetch_news
from .order_executor import OrderExecutor
from .probability_estimator import estimate_probability
from .risk_manager import RiskManager
from .tracker import Tracker

logger = logging.getLogger(__name__)

CLOB_BASE_URL = "https://clob.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"


async def fetch_resolved_markets(
    limit: int = 50,
    category: str = "",
) -> list[dict]:
    """Fetch markets that have already resolved (closed with known outcomes).

    Uses Polymarket Gamma API which provides richer market metadata
    including resolution data.
    """
    params: dict = {
        "limit": limit,
        "closed": "true",
        "order": "end_date_iso",
        "ascending": "false",
    }
    if category:
        params["tag"] = category

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(f"{GAMMA_API_URL}/markets", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("Failed to fetch resolved markets: %s", e)
            return []

    raw_markets = resp.json()
    if not isinstance(raw_markets, list):
        raw_markets = raw_markets.get("data", [])

    resolved = []
    for m in raw_markets:
        outcome = _parse_outcome(m)
        if outcome is None:
            continue

        # Only include binary YES/NO markets
        tokens = m.get("tokens", m.get("clobTokenIds", []))
        if isinstance(tokens, list) and len(tokens) != 2:
            # Check if it's a simple yes/no from outcomes field
            outcomes_list = m.get("outcomes", [])
            if not (len(outcomes_list) == 2 and "Yes" in str(outcomes_list)):
                continue

        end_date_str = m.get("end_date_iso", m.get("endDate", ""))
        if not end_date_str:
            continue

        try:
            end_date = datetime.fromisoformat(
                end_date_str.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            continue

        # Get the historical YES price at close
        close_price = _parse_close_price(m)

        resolved.append({
            "market_id": m.get("condition_id", m.get("id", "")),
            "question": m.get("question", ""),
            "category": (m.get("category", "") or "").lower(),
            "end_date": end_date,
            "close_price": close_price,
            "outcome": outcome,  # 1.0 = YES resolved, 0.0 = NO resolved
            "volume": float(m.get("volume", m.get("volumeNum", 0)) or 0),
        })

    logger.info("Found %d resolved markets for backtesting", len(resolved))
    return resolved


def _parse_outcome(market: dict) -> Optional[float]:
    """Determine market outcome from resolution data.

    Returns 1.0 if YES, 0.0 if NO, None if unresolved.
    """
    # Check various resolution fields
    resolution = market.get("resolution", market.get("resolved_to", ""))
    if isinstance(resolution, str):
        res_lower = resolution.lower()
        if res_lower in ("yes", "true", "1"):
            return 1.0
        if res_lower in ("no", "false", "0"):
            return 0.0

    # Check outcome_prices
    outcome_prices = market.get("outcomePrices", market.get("outcome_prices", ""))
    if isinstance(outcome_prices, str) and outcome_prices:
        try:
            prices = json.loads(outcome_prices)
            if isinstance(prices, list) and len(prices) >= 2:
                # First price is YES, second is NO
                yes_price = float(prices[0])
                if yes_price > 0.9:
                    return 1.0
                if yes_price < 0.1:
                    return 0.0
        except (json.JSONDecodeError, ValueError):
            pass
    elif isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
        yes_price = float(outcome_prices[0])
        if yes_price > 0.9:
            return 1.0
        if yes_price < 0.1:
            return 0.0

    # Check winner field
    winner = market.get("winner", "")
    if winner:
        if str(winner).lower() in ("yes", "0"):  # 0 index = YES token
            return 1.0
        if str(winner).lower() in ("no", "1"):
            return 0.0

    return None


def _parse_close_price(market: dict) -> float:
    """Get the last known YES price before market closed."""
    # Try outcomePrices
    outcome_prices = market.get("outcomePrices", market.get("outcome_prices", ""))
    if isinstance(outcome_prices, str) and outcome_prices:
        try:
            prices = json.loads(outcome_prices)
            if isinstance(prices, list) and len(prices) >= 1:
                return float(prices[0])
        except (json.JSONDecodeError, ValueError):
            pass

    # Try tokens
    tokens = market.get("tokens", [])
    if isinstance(tokens, list):
        for t in tokens:
            if isinstance(t, dict) and t.get("outcome", "").upper() == "YES":
                return float(t.get("price", 0.5))

    # Try bestBid / bestAsk
    best_bid = market.get("bestBid", 0)
    best_ask = market.get("bestAsk", 0)
    if best_bid and best_ask:
        return (float(best_bid) + float(best_ask)) / 2

    return 0.5  # fallback


async def run_backtest(
    tracker: Tracker,
    num_markets: int = 20,
    category: str = "",
    use_news: bool = True,
) -> dict:
    """Run a full backtest on resolved markets.

    Steps:
    1. Fetch resolved markets from Polymarket
    2. For each market, simulate the prediction pipeline:
       a. Fetch news with cutoff_date = market.end_date (temporal correctness)
       b. Call Claude to estimate probability
       c. Calculate edge and find opportunities
       d. Simulate trades
    3. Score predictions against actual outcomes
    4. Report Brier Score, calibration, and simulated P&L

    Args:
        tracker: Tracker instance for persistence.
        num_markets: How many resolved markets to backtest.
        category: Filter markets by category.
        use_news: Whether to fetch news (set False for faster dry runs).
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    news_key = os.environ.get("NEWS_API_KEY", "")

    if not anthropic_key:
        return {"error": "ANTHROPIC_API_KEY required for backtesting"}

    # 1. Fetch resolved markets
    logger.info("Fetching resolved markets for backtest...")
    resolved = await fetch_resolved_markets(limit=num_markets, category=category)

    if not resolved:
        return {"error": "No resolved markets found"}

    risk_manager = RiskManager()
    executor = OrderExecutor(
        risk_manager=risk_manager,
        tracker=tracker,
        live=False,
    )

    results = {
        "markets_tested": 0,
        "predictions_made": 0,
        "trades_simulated": 0,
        "brier_scores": [],
        "correct_direction": 0,
        "market_details": [],
    }

    for rm in resolved:
        logger.info(
            "Backtesting: %s (outcome=%s)",
            rm["question"][:60],
            "YES" if rm["outcome"] == 1.0 else "NO",
        )

        # Build Market object — use close_price as the "current" price
        # at the time we would have made our prediction
        # Simulate making prediction 7 days before close
        prediction_date = rm["end_date"] - timedelta(days=7)

        market = Market(
            market_id=rm["market_id"],
            question=rm["question"],
            yes_price=rm["close_price"],
            volume_24h=rm.get("volume", 5000.0),
            end_date=rm["end_date"],
            category=rm["category"] or "unknown",
        )

        # 2. Fetch news with temporal cutoff
        articles = []
        if use_news and news_key:
            articles = await fetch_news(
                question=market.question,
                api_key=news_key,
                days_back=7,
                category=market.category,
                cutoff_date=prediction_date,
            )
        elif use_news:
            # Use free sources only
            articles = await fetch_news(
                question=market.question,
                api_key="",
                days_back=7,
                category=market.category,
                cutoff_date=prediction_date,
            )

        # 3. Estimate probability via Claude
        prediction = await estimate_probability(
            market_id=market.market_id,
            question=market.question,
            end_date=market.end_date,
            yes_price=market.yes_price,
            articles=articles,
            api_key=anthropic_key,
        )

        # Save prediction
        tracker.save_prediction(prediction)
        results["predictions_made"] += 1

        # 4. Find opportunities and simulate trades
        opportunities = find_opportunities([market], [prediction])

        for opp in opportunities:
            if risk_manager.can_trade():
                trade = executor.execute(opp)
                if trade:
                    # Immediately resolve with known outcome
                    actual_outcome = rm["outcome"]
                    trade_won = (
                        (trade.direction == "YES" and actual_outcome == 1.0)
                        or (trade.direction == "NO" and actual_outcome == 0.0)
                    )
                    trade.outcome = 1.0 if trade_won else 0.0
                    tracker.update_trade_outcome(trade.trade_id, trade.outcome)
                    risk_manager.update_trade(trade.trade_id, trade.outcome)
                    results["trades_simulated"] += 1

        # 5. Calculate per-market Brier score
        brier = (prediction.claude_probability - rm["outcome"]) ** 2
        results["brier_scores"].append(brier)

        # Track direction accuracy
        predicted_yes = prediction.claude_probability > 0.5
        actual_yes = rm["outcome"] == 1.0
        if predicted_yes == actual_yes:
            results["correct_direction"] += 1

        results["markets_tested"] += 1

        results["market_details"].append({
            "question": rm["question"][:80],
            "outcome": "YES" if rm["outcome"] == 1.0 else "NO",
            "claude_prob": round(prediction.claude_probability, 3),
            "market_price": round(rm["close_price"], 3),
            "brier": round(brier, 4),
            "edge": round(prediction.edge, 4),
            "confidence": prediction.confidence,
            "news_count": len(articles),
            "correct": predicted_yes == actual_yes,
        })

    # 6. Aggregate metrics
    if results["brier_scores"]:
        results["brier_score_mean"] = round(
            sum(results["brier_scores"]) / len(results["brier_scores"]), 4
        )
    else:
        results["brier_score_mean"] = None

    results["direction_accuracy"] = (
        round(results["correct_direction"] / results["markets_tested"], 3)
        if results["markets_tested"] > 0
        else None
    )

    # Get tracker's overall metrics
    tracker_brier = tracker.calculate_brier_score()
    calibration = tracker.calculate_calibration()

    results["tracker_brier_score"] = tracker_brier
    results["calibration"] = calibration

    # Clean up intermediate data
    del results["brier_scores"]

    return results


def format_backtest_report(results: dict) -> str:
    """Format backtest results into a readable report."""
    if "error" in results:
        return f"Backtest failed: {results['error']}"

    lines = [
        "=" * 60,
        "  POLYMARKET BOT — BACKTEST REPORT",
        "=" * 60,
        "",
        f"  Markets tested:     {results['markets_tested']}",
        f"  Predictions made:   {results['predictions_made']}",
        f"  Trades simulated:   {results['trades_simulated']}",
        "",
        f"  Mean Brier Score:   {results.get('brier_score_mean', 'N/A')}",
        f"    (target: < 0.20, coin flip = 0.25, perfect = 0.00)",
        "",
        f"  Direction accuracy: {_pct(results.get('direction_accuracy'))}",
        "",
    ]

    # Calibration
    cal = results.get("calibration", {})
    if cal:
        lines.append("  Calibration:")
        for bucket, data in sorted(cal.items()):
            lines.append(
                f"    {bucket}: actual={data['actual_rate']:.1%} "
                f"(n={data['count']})"
            )
        lines.append("")

    # Per-market details
    details = results.get("market_details", [])
    if details:
        lines.append("  Market Details:")
        lines.append("  " + "-" * 56)
        for d in details:
            marker = "✓" if d["correct"] else "✗"
            lines.append(
                f"  {marker} {d['question']}"
            )
            lines.append(
                f"      Outcome: {d['outcome']}  |  Claude: {d['claude_prob']:.1%}  "
                f"|  Market: {d['market_price']:.1%}  |  Brier: {d['brier']:.4f}  "
                f"|  News: {d['news_count']}"
            )
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:.1%}"
