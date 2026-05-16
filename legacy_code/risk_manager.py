"""Risk management: position limits, daily loss limits, halt conditions."""

import logging
from datetime import datetime

from .models import Trade

logger = logging.getLogger(__name__)

MAX_BET_SIZE = 1.00  # $1.00 per individual bet
MAX_TOTAL_EXPOSURE = 10.00  # $10.00 total open positions
DAILY_LOSS_LIMIT = 4.00  # $4.00 daily loss → HALT


class RiskManager:
    def __init__(self) -> None:
        self._trades: list[Trade] = []
        self._halted = False
        self._halt_reason: str | None = None

    def register_trade(self, trade: Trade) -> None:
        self._trades.append(trade)
        self._check_halt_conditions()

    def update_trade(self, trade_id: str, outcome: float) -> None:
        for t in self._trades:
            if t.trade_id == trade_id:
                t.outcome = outcome
                t.status = "filled"
                break
        self._check_halt_conditions()

    def can_trade(self) -> bool:
        if self._halted:
            logger.warning("Trading HALTED: %s", self._halt_reason)
            return False
        return True

    def validate_trade(self, trade: Trade) -> tuple[bool, str]:
        """Validate a proposed trade against risk limits."""
        if self._halted:
            return False, f"Trading halted: {self._halt_reason}"

        if trade.size > MAX_BET_SIZE:
            return False, f"Bet size ${trade.size:.2f} exceeds max ${MAX_BET_SIZE:.2f}"

        exposure = self.current_exposure()
        cost = self._trade_cost(trade)
        if exposure + cost > MAX_TOTAL_EXPOSURE:
            return (
                False,
                f"Would exceed max exposure: ${exposure:.2f} + ${cost:.2f} > ${MAX_TOTAL_EXPOSURE:.2f}",
            )

        return True, "OK"

    def current_exposure(self) -> float:
        """Total capital at risk in open positions."""
        total = 0.0
        for t in self._trades:
            if t.outcome is None and t.status not in ("cancelled",):
                total += self._trade_cost(t)
        return round(total, 2)

    def daily_pnl(self) -> float:
        """Calculate today's realized P&L."""
        today = datetime.utcnow().date()
        pnl = 0.0
        for t in self._trades:
            if t.timestamp.date() != today:
                continue
            if t.outcome is None:
                continue
            if t.outcome == 1.0:
                if t.direction == "YES":
                    pnl += (1.0 - t.limit_price) * t.size
                else:
                    pnl += t.limit_price * t.size
            else:
                if t.direction == "YES":
                    pnl -= t.limit_price * t.size
                else:
                    pnl -= (1.0 - t.limit_price) * t.size
        return round(pnl, 2)

    def _trade_cost(self, trade: Trade) -> float:
        """Cost of entering a trade."""
        if trade.direction == "YES":
            return round(trade.limit_price * trade.size, 2)
        else:
            return round((1.0 - trade.limit_price) * trade.size, 2)

    def _check_halt_conditions(self) -> None:
        pnl = self.daily_pnl()
        if pnl <= -DAILY_LOSS_LIMIT:
            self._halted = True
            self._halt_reason = f"Daily loss limit hit: ${pnl:.2f}"
            logger.critical("HALT TRIGGERED: %s", self._halt_reason)

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    def reset_daily(self) -> None:
        """Reset halt status for a new trading day."""
        self._halted = False
        self._halt_reason = None
        logger.info("Daily risk limits reset")
