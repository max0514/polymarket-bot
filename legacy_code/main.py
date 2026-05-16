"""Main entry point: scheduler, CLI, and orchestration."""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import click
from dotenv import load_dotenv

from .backtester import format_backtest_report, run_backtest
from .edge_calculator import find_opportunities
from .market_scanner import fetch_markets
from .models import Prediction
from .news_fetcher import fetch_news
from .order_executor import OrderExecutor
from .probability_estimator import estimate_probability
from .risk_manager import RiskManager
from .tracker import Tracker

load_dotenv()

# Structured JSON logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def log_structured(**kwargs) -> None:
    """Emit a structured JSON log line."""
    kwargs["timestamp"] = datetime.utcnow().isoformat()
    logger.info(json.dumps(kwargs))


async def run_cycle(
    tracker: Tracker,
    risk_manager: RiskManager,
    live: bool = False,
    llm_provider: str = "",
    llm_model: str = "",
) -> dict:
    """Run one full scan → estimate → trade cycle."""
    provider = llm_provider or os.environ.get("LLM_PROVIDER", "claude")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    news_key = os.environ.get("NEWS_API_KEY", "")
    polymarket_key = os.environ.get("POLYMARKET_API_KEY")

    if provider == "claude" and not anthropic_key:
        log_structured(action="error", message="ANTHROPIC_API_KEY not set")
        return {"error": "ANTHROPIC_API_KEY not set"}

    # 1. Scan markets
    log_structured(action="scan_start")
    markets = await fetch_markets(api_key=polymarket_key)
    log_structured(action="scan_complete", markets_found=len(markets))

    if not markets:
        log_structured(action="no_markets", message="No qualifying markets found")
        return {"markets": 0, "trades": 0}

    # 2. Fetch news and estimate probabilities
    predictions: list[Prediction] = []
    for market in markets:
        log_structured(
            action="processing_market",
            market_id=market.market_id,
            question=market.question[:80],
        )

        # Fetch news (multi-source: free + paid if key provided)
        articles = await fetch_news(
            question=market.question,
            api_key=news_key,
            category=market.category,
        )

        if len(articles) < 3:
            log_structured(
                action="low_news_quality",
                market_id=market.market_id,
                article_count=len(articles),
            )

        # Estimate probability
        prediction = await estimate_probability(
            market_id=market.market_id,
            question=market.question,
            end_date=market.end_date,
            yes_price=market.yes_price,
            articles=articles,
            api_key=anthropic_key,
            provider=provider,
            model=llm_model,
        )
        predictions.append(prediction)
        tracker.save_prediction(prediction)

    # 3. Find opportunities
    opportunities = find_opportunities(markets, predictions)
    log_structured(action="opportunities_found", count=len(opportunities))

    # 4. Execute trades
    executor = OrderExecutor(
        risk_manager=risk_manager,
        tracker=tracker,
        live=live,
        api_key=polymarket_key,
    )

    trades_executed = 0
    for opp in opportunities:
        if not risk_manager.can_trade():
            log_structured(action="halt", reason=risk_manager.halt_reason)
            break

        trade = executor.execute(opp)
        if trade:
            trades_executed += 1
            log_structured(
                action="trade_executed",
                market_id=opp.market.market_id,
                direction=trade.direction,
                size=trade.size,
                limit_price=trade.limit_price,
                status=trade.status,
                edge_pct=opp.edge_pct,
                reasoning=opp.prediction.reasoning[:200],
            )

    return {
        "markets": len(markets),
        "predictions": len(predictions),
        "opportunities": len(opportunities),
        "trades": trades_executed,
    }


@click.command()
@click.option("--simulate", is_flag=True, default=True, help="Run in simulation mode (default)")
@click.option("--live", is_flag=True, default=False, help="Enable live trading")
@click.option("--report", is_flag=True, default=False, help="Generate performance report")
@click.option("--backtest", is_flag=True, default=False, help="Run backtest on historical data")
@click.option("--backtest-markets", default=20, type=int, help="Number of markets to backtest")
@click.option("--backtest-category", default="", help="Filter backtest by category")
@click.option("--once", is_flag=True, default=False, help="Run once instead of on schedule")
@click.option("--llm-provider", default="", help="LLM provider: 'claude' (default) or 'ollama'")
@click.option("--llm-model", default="", help="Model name (e.g. qwen3.5:122b, llama3.3, claude-sonnet-4-20250514)")
def main(
    simulate: bool,
    live: bool,
    report: bool,
    backtest: bool,
    backtest_markets: int,
    backtest_category: str,
    once: bool,
    llm_provider: str,
    llm_model: str,
) -> None:
    """Polymarket AI Prediction Trading Bot."""
    tracker = Tracker()
    risk_manager = RiskManager()

    if report:
        rpt = tracker.generate_report()
        click.echo(json.dumps(rpt, indent=2))
        return

    # Resolve provider for display
    provider = llm_provider or os.environ.get("LLM_PROVIDER", "claude")
    model = llm_model or os.environ.get(
        "OLLAMA_MODEL" if provider == "ollama" else "CLAUDE_MODEL", ""
    )
    if provider == "ollama":
        click.echo(f"Using local LLM: {model or 'qwen3.5:122b'} via Ollama")
    elif model:
        click.echo(f"Using Claude model: {model}")

    if backtest:
        click.echo("Running backtest on resolved markets...")
        results = asyncio.run(run_backtest(
            tracker=tracker,
            num_markets=backtest_markets,
            category=backtest_category,
        ))
        click.echo(format_backtest_report(results))
        return

    # Safety: require explicit opt-in for live trading
    if live:
        env_live = os.environ.get("LIVE_TRADING", "false").lower()
        if env_live != "true":
            click.echo(
                "ERROR: Live trading requires LIVE_TRADING=true environment variable."
            )
            sys.exit(1)
        click.echo("⚠️  LIVE TRADING ENABLED — real money at risk!")
    else:
        click.echo("Running in SIMULATION mode (no real money)")

    if once:
        result = asyncio.run(run_cycle(
            tracker, risk_manager, live=live,
            llm_provider=llm_provider, llm_model=llm_model,
        ))
        click.echo(json.dumps(result, indent=2))
    else:
        _run_scheduled(
            tracker, risk_manager, live=live,
            llm_provider=llm_provider, llm_model=llm_model,
        )


def _run_scheduled(
    tracker: Tracker,
    risk_manager: RiskManager,
    live: bool,
    llm_provider: str = "",
    llm_model: str = "",
) -> None:
    """Run the bot on a 4-hour schedule."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler()

    def job():
        risk_manager.reset_daily()  # Reset at start of each cycle
        result = asyncio.run(run_cycle(
            tracker, risk_manager, live=live,
            llm_provider=llm_provider, llm_model=llm_model,
        ))
        log_structured(action="cycle_complete", **result)

    # Run immediately, then every 4 hours
    scheduler.add_job(job, "interval", hours=4, next_run_time=datetime.utcnow())

    try:
        click.echo("Bot started — running every 4 hours. Press Ctrl+C to stop.")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        click.echo("\nBot stopped.")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
