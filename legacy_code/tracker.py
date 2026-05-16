"""Prediction tracker with SQLite storage and Brier Score calculation."""

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Prediction, Trade

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    claude_probability REAL NOT NULL,
    market_price REAL NOT NULL,
    edge REAL NOT NULL,
    confidence TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    bayesian_prior REAL NOT NULL,
    key_evidence TEXT NOT NULL,
    risks TEXT NOT NULL,
    news_articles TEXT NOT NULL,
    news_quality_score INTEGER
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    size REAL NOT NULL,
    limit_price REAL NOT NULL,
    status TEXT NOT NULL,
    outcome REAL,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id)
);

CREATE INDEX IF NOT EXISTS idx_predictions_market ON predictions(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
"""


class Tracker:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def save_prediction(self, prediction: Prediction) -> str:
        pred_id = prediction.prediction_id or str(uuid.uuid4())
        prediction.prediction_id = pred_id

        with self._connect() as conn:
            conn.execute(
                """INSERT INTO predictions
                   (prediction_id, market_id, timestamp, claude_probability,
                    market_price, edge, confidence, reasoning, bayesian_prior,
                    key_evidence, risks, news_articles, news_quality_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pred_id,
                    prediction.market_id,
                    prediction.timestamp.isoformat(),
                    prediction.claude_probability,
                    prediction.market_price,
                    prediction.edge,
                    prediction.confidence,
                    prediction.reasoning,
                    prediction.bayesian_prior,
                    json.dumps(prediction.key_evidence),
                    json.dumps(prediction.risks),
                    json.dumps(prediction.news_articles),
                    prediction.news_quality_score,
                ),
            )
        logger.info("Saved prediction %s for market %s", pred_id, prediction.market_id)
        return pred_id

    def save_trade(self, trade: Trade) -> str:
        trade_id = trade.trade_id or str(uuid.uuid4())
        trade.trade_id = trade_id

        with self._connect() as conn:
            conn.execute(
                """INSERT INTO trades
                   (trade_id, prediction_id, direction, size, limit_price,
                    status, outcome, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade_id,
                    trade.prediction_id,
                    trade.direction,
                    trade.size,
                    trade.limit_price,
                    trade.status,
                    trade.outcome,
                    trade.timestamp.isoformat(),
                ),
            )
        logger.info("Saved trade %s (status=%s)", trade_id, trade.status)
        return trade_id

    def update_trade_outcome(self, trade_id: str, outcome: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE trades SET outcome = ?, status = 'filled' WHERE trade_id = ?",
                (outcome, trade_id),
            )

    def get_all_predictions(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM predictions ORDER BY timestamp DESC").fetchall()
        return [dict(r) for r in rows]

    def get_all_trades(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT t.*, p.claude_probability, p.market_price
                   FROM trades t
                   JOIN predictions p ON t.prediction_id = p.prediction_id
                   ORDER BY t.timestamp DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    def get_resolved_predictions(self) -> list[dict]:
        """Get predictions that have trades with known outcomes."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT p.claude_probability, t.outcome, t.direction
                   FROM predictions p
                   JOIN trades t ON p.prediction_id = t.prediction_id
                   WHERE t.outcome IS NOT NULL"""
            ).fetchall()
        return [dict(r) for r in rows]

    def calculate_brier_score(self) -> Optional[float]:
        """Calculate Brier Score: BS = (1/N) * Σ(forecast - outcome)².

        Lower is better. Perfect = 0.0, worst = 1.0, coin flip = 0.25.
        """
        resolved = self.get_resolved_predictions()
        if not resolved:
            return None

        total = 0.0
        for r in resolved:
            prob = r["claude_probability"]
            # If we bet NO, the relevant probability is 1 - claude_probability
            if r["direction"] == "NO":
                prob = 1.0 - prob
            outcome = r["outcome"]
            total += (prob - outcome) ** 2

        return round(total / len(resolved), 4)

    def calculate_calibration(self, bucket_size: float = 0.1) -> dict[str, dict]:
        """Check calibration: when Claude says X%, does it resolve ~X%?

        Groups predictions into buckets and compares predicted vs actual rates.
        """
        resolved = self.get_resolved_predictions()
        if not resolved:
            return {}

        buckets: dict[str, list[float]] = {}
        for r in resolved:
            prob = r["claude_probability"]
            bucket_start = int(prob / bucket_size) * bucket_size
            bucket_label = f"{bucket_start:.0%}-{bucket_start + bucket_size:.0%}"
            if bucket_label not in buckets:
                buckets[bucket_label] = []
            buckets[bucket_label].append(r["outcome"])

        result = {}
        for label, outcomes in sorted(buckets.items()):
            actual_rate = sum(outcomes) / len(outcomes)
            result[label] = {
                "count": len(outcomes),
                "actual_rate": round(actual_rate, 3),
            }
        return result

    def daily_pnl(self) -> float:
        """Calculate today's P&L from resolved trades."""
        today = datetime.utcnow().date().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT t.size, t.outcome, t.limit_price, t.direction
                   FROM trades t
                   WHERE t.outcome IS NOT NULL
                   AND date(t.timestamp) = ?""",
                (today,),
            ).fetchall()

        pnl = 0.0
        for r in rows:
            if r["outcome"] == 1.0:
                # Won: payout is (1.0 - limit_price) * size for YES,
                # or (limit_price) * size for NO
                if r["direction"] == "YES":
                    pnl += (1.0 - r["limit_price"]) * r["size"]
                else:
                    pnl += r["limit_price"] * r["size"]
            else:
                # Lost: lose the cost
                if r["direction"] == "YES":
                    pnl -= r["limit_price"] * r["size"]
                else:
                    pnl -= (1.0 - r["limit_price"]) * r["size"]
        return round(pnl, 2)

    def generate_report(self) -> dict:
        """Generate a daily summary report."""
        brier = self.calculate_brier_score()
        calibration = self.calculate_calibration()

        with self._connect() as conn:
            total_predictions = conn.execute(
                "SELECT COUNT(*) FROM predictions"
            ).fetchone()[0]
            total_trades = conn.execute(
                "SELECT COUNT(*) FROM trades"
            ).fetchone()[0]
            resolved_trades = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE outcome IS NOT NULL"
            ).fetchone()[0]
            open_positions = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE outcome IS NULL AND status != 'cancelled'"
            ).fetchone()[0]

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "total_predictions": total_predictions,
            "total_trades": total_trades,
            "resolved_trades": resolved_trades,
            "open_positions": open_positions,
            "brier_score": brier,
            "calibration": calibration,
            "daily_pnl": self.daily_pnl(),
        }
