"""Order execution: simulation (default) and live mode."""

import logging
import uuid
from datetime import datetime

import httpx

from .models import Trade, TradeOpportunity
from .risk_manager import RiskManager
from .tracker import Tracker

logger = logging.getLogger(__name__)

CLOB_BASE_URL = "https://clob.polymarket.com"
SPREAD_OFFSET = 0.005  # 0.5% tight spread for limit orders


class OrderExecutor:
    def __init__(
        self,
        risk_manager: RiskManager,
        tracker: Tracker,
        live: bool = False,
        api_key: str | None = None,
    ):
        self.risk_manager = risk_manager
        self.tracker = tracker
        self.live = live
        self.api_key = api_key

        if self.live and not self.api_key:
            raise ValueError("API key required for live trading")

    def execute(self, opportunity: TradeOpportunity) -> Trade | None:
        """Execute a trade for a given opportunity."""
        if not self.risk_manager.can_trade():
            logger.warning("Risk manager blocked trading")
            return None

        # Calculate limit price with tight spread
        market_price = opportunity.market.yes_price
        if opportunity.direction == "YES":
            limit_price = round(market_price + SPREAD_OFFSET, 2)
        else:
            limit_price = round(market_price - SPREAD_OFFSET, 2)

        # Clamp to valid range
        limit_price = max(0.01, min(0.99, limit_price))

        trade = Trade(
            trade_id=str(uuid.uuid4()),
            prediction_id=opportunity.prediction.prediction_id or "",
            direction=opportunity.direction,
            size=opportunity.suggested_size,
            limit_price=limit_price,
            status="simulated" if not self.live else "pending",
            timestamp=datetime.utcnow(),
        )

        # Validate against risk limits
        valid, reason = self.risk_manager.validate_trade(trade)
        if not valid:
            logger.warning("Trade rejected by risk manager: %s", reason)
            return None

        if self.live:
            return self._execute_live(trade, opportunity)
        else:
            return self._execute_simulation(trade, opportunity)

    def _execute_simulation(
        self, trade: Trade, opportunity: TradeOpportunity
    ) -> Trade:
        """Log simulated trade."""
        trade.status = "simulated"

        logger.info(
            'SIMULATION: Would have bought %s %s at $%.2f size $%.2f (edge=%.1f%%)',
            opportunity.market.market_id,
            trade.direction,
            trade.limit_price,
            trade.size,
            opportunity.edge_pct,
        )

        self.risk_manager.register_trade(trade)
        self.tracker.save_trade(trade)
        return trade

    def _execute_live(
        self, trade: Trade, opportunity: TradeOpportunity
    ) -> Trade | None:
        """Place a limit order via Polymarket CLOB API."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        order_payload = {
            "market": opportunity.market.market_id,
            "side": trade.direction,
            "size": trade.size,
            "price": trade.limit_price,
            "type": "limit",
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    f"{CLOB_BASE_URL}/order",
                    headers=headers,
                    json=order_payload,
                )
                resp.raise_for_status()

            trade.status = "pending"
            logger.info(
                "LIVE ORDER PLACED: %s %s at $%.2f size $%.2f",
                opportunity.market.market_id,
                trade.direction,
                trade.limit_price,
                trade.size,
            )

        except httpx.HTTPError as e:
            logger.error("Order failed: %s", e)
            trade.status = "cancelled"

        self.risk_manager.register_trade(trade)
        self.tracker.save_trade(trade)
        return trade
