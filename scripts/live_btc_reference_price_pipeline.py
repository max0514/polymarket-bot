"""Live BTC reference-price mismatch pipeline.

Collects the two BTC reference prices that matter for Kalshi vs Polymarket
15-minute BTC Up/Down markets:

  - Kalshi side: CF Benchmarks BRTI
  - Polymarket side: Polymarket RTDS Chainlink btc/usd stream

The script stores raw price observations and pairwise mismatch checks in SQLite.

Run:
  python3 scripts/live_btc_reference_price_pipeline.py
  python3 scripts/live_btc_reference_price_pipeline.py --once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import websockets


DEFAULT_DB = Path("data/live_orderbooks/btc_reference_prices.sqlite")
BRTI_PRICE_URL = "https://www.cfbenchmarks.com/data/indices/BRTI"
POLYMARKET_RTDS_URL = "wss://ws-live-data.polymarket.com"
POLYMARKET_RTDS_SYMBOL = "btc/usd"


@dataclass
class PriceObservation:
    source: str
    symbol: str
    price: float
    observed_ms: int
    source_ms: int | None
    raw_json: str


def utc_now_ms() -> int:
    return int(time.time() * 1000)


def utc_iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS price_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            observed_ms INTEGER NOT NULL,
            observed_utc TEXT NOT NULL,
            source_ms INTEGER,
            source_utc TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_price_observations_source_time
            ON price_observations(source, observed_ms);

        CREATE TABLE IF NOT EXISTS mismatch_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_ms INTEGER NOT NULL,
            checked_utc TEXT NOT NULL,
            kalshi_source TEXT NOT NULL,
            kalshi_price REAL NOT NULL,
            kalshi_observed_ms INTEGER NOT NULL,
            polymarket_source TEXT NOT NULL,
            polymarket_price REAL NOT NULL,
            polymarket_observed_ms INTEGER NOT NULL,
            price_diff REAL NOT NULL,
            abs_price_diff REAL NOT NULL,
            pct_diff REAL NOT NULL,
            time_skew_ms INTEGER NOT NULL,
            mismatch_threshold REAL NOT NULL,
            stale_after_ms INTEGER NOT NULL,
            is_mismatch INTEGER NOT NULL,
            action TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mismatch_checks_time
            ON mismatch_checks(checked_ms);
        """
    )
    conn.commit()
    return conn


def parse_btc_price_text(text: str) -> float | None:
    """Extract a plausible BTC/USD price from HTML or JSON-like text."""
    patterns = [
        r'"value"\s*:\s*"?([0-9]{4,6}(?:\.[0-9]+)?)"?',
        r'"price"\s*:\s*"?([0-9]{4,6}(?:\.[0-9]+)?)"?',
        r"\b([0-9]{4,6}(?:\.[0-9]+)?)\b",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text):
            price = float(match)
            if 10_000 <= price <= 250_000:
                return price
    return None


async def fetch_brti(client: httpx.AsyncClient, url: str) -> PriceObservation:
    observed_ms = utc_now_ms()
    response = await client.get(url)
    response.raise_for_status()
    text = response.text
    price = parse_btc_price_text(text)
    if price is None:
        raise RuntimeError("Could not parse BRTI BTC price")
    return PriceObservation(
        source="kalshi_brti",
        symbol="btc/usd",
        price=price,
        observed_ms=observed_ms,
        source_ms=None,
        raw_json=json.dumps({"url": url, "body_prefix": text[:2000]}),
    )


def polymarket_subscribe_message(symbol: str) -> str:
    return json.dumps(
        {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": json.dumps({"symbol": symbol}),
                }
            ],
        }
    )


def parse_polymarket_payload(message: dict[str, Any], symbol: str) -> PriceObservation | None:
    if message.get("topic") not in ("crypto_prices", "crypto_prices_chainlink"):
        return None
    payload = message.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        payload = payload["data"]
    items = payload if isinstance(payload, list) else [payload]
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        item_symbol = str(item.get("symbol") or symbol).lower()
        if item_symbol != symbol.lower():
            continue
        value = item.get("value")
        if value is None:
            continue
        source_ms = item.get("timestamp")
        if source_ms is not None:
            source_ms = int(source_ms)
        return PriceObservation(
            source="polymarket_rtds_chainlink",
            symbol=item_symbol,
            price=float(value),
            observed_ms=utc_now_ms(),
            source_ms=source_ms,
            raw_json=json.dumps(message, separators=(",", ":")),
        )
    return None


async def fetch_polymarket_rtds(
    url: str,
    symbol: str,
    timeout_seconds: float,
) -> PriceObservation:
    async with websockets.connect(url, ping_interval=None, open_timeout=timeout_seconds) as ws:
        await ws.send(polymarket_subscribe_message(symbol))
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.25, deadline - time.monotonic()))
            if isinstance(raw, str) and raw.strip().upper() in {"PING", "PONG"}:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            observation = parse_polymarket_payload(message, symbol)
            if observation:
                return observation
    raise TimeoutError("No Polymarket RTDS BTC price received")


def insert_observation(conn: sqlite3.Connection, obs: PriceObservation) -> int:
    cursor = conn.execute(
        """
        INSERT INTO price_observations (
            source, symbol, price, observed_ms, observed_utc, source_ms, source_utc, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            obs.source,
            obs.symbol,
            obs.price,
            obs.observed_ms,
            utc_iso_from_ms(obs.observed_ms),
            obs.source_ms,
            utc_iso_from_ms(obs.source_ms) if obs.source_ms else None,
            obs.raw_json,
        ),
    )
    return int(cursor.lastrowid)


def risk_action(abs_price_diff: float, time_skew_ms: int, threshold: float, stale_after_ms: int) -> str:
    if abs(time_skew_ms) > stale_after_ms:
        return "pause_trading_stale_source"
    if abs_price_diff >= threshold * 3:
        return "exit_or_hedge_immediately"
    if abs_price_diff >= threshold * 2:
        return "reduce_position_and_disable_new_entries"
    if abs_price_diff >= threshold:
        return "disable_new_entries"
    return "ok"


def insert_mismatch_check(
    conn: sqlite3.Connection,
    kalshi: PriceObservation,
    polymarket: PriceObservation,
    threshold: float,
    stale_after_ms: int,
) -> dict[str, Any]:
    checked_ms = utc_now_ms()
    price_diff = kalshi.price - polymarket.price
    abs_price_diff = abs(price_diff)
    pct_diff = abs_price_diff / ((kalshi.price + polymarket.price) / 2)
    time_skew_ms = kalshi.observed_ms - polymarket.observed_ms
    is_mismatch = int(abs_price_diff >= threshold or abs(time_skew_ms) > stale_after_ms)
    action = risk_action(abs_price_diff, time_skew_ms, threshold, stale_after_ms)
    conn.execute(
        """
        INSERT INTO mismatch_checks (
            checked_ms, checked_utc, kalshi_source, kalshi_price, kalshi_observed_ms,
            polymarket_source, polymarket_price, polymarket_observed_ms,
            price_diff, abs_price_diff, pct_diff, time_skew_ms,
            mismatch_threshold, stale_after_ms, is_mismatch, action
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            checked_ms,
            utc_iso_from_ms(checked_ms),
            kalshi.source,
            kalshi.price,
            kalshi.observed_ms,
            polymarket.source,
            polymarket.price,
            polymarket.observed_ms,
            price_diff,
            abs_price_diff,
            pct_diff,
            time_skew_ms,
            threshold,
            stale_after_ms,
            is_mismatch,
            action,
        ),
    )
    return {
        "checked_utc": utc_iso_from_ms(checked_ms),
        "kalshi_price": kalshi.price,
        "polymarket_price": polymarket.price,
        "price_diff": price_diff,
        "abs_price_diff": abs_price_diff,
        "pct_diff": pct_diff,
        "time_skew_ms": time_skew_ms,
        "is_mismatch": bool(is_mismatch),
        "action": action,
    }


async def collect_once(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    args: argparse.Namespace,
) -> dict[str, Any]:
    brti_task = fetch_brti(client, args.brti_url)
    rtds_task = fetch_polymarket_rtds(
        args.polymarket_rtds_url,
        args.polymarket_symbol,
        args.rtds_timeout_seconds,
    )
    kalshi_obs, polymarket_obs = await asyncio.gather(brti_task, rtds_task)
    insert_observation(conn, kalshi_obs)
    insert_observation(conn, polymarket_obs)
    check = insert_mismatch_check(
        conn,
        kalshi_obs,
        polymarket_obs,
        args.mismatch_threshold,
        args.stale_after_ms,
    )
    conn.commit()
    return check


async def run(args: argparse.Namespace) -> None:
    conn = init_db(args.db)
    stop = asyncio.Event()

    def request_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)

    async with httpx.AsyncClient(timeout=args.http_timeout) as client:
        while not stop.is_set():
            started = time.monotonic()
            try:
                check = await collect_once(client, conn, args)
                print(
                    f"{check['checked_utc']} "
                    f"BRTI={check['kalshi_price']:.2f} "
                    f"RTDS={check['polymarket_price']:.2f} "
                    f"diff={check['price_diff']:+.2f} "
                    f"abs={check['abs_price_diff']:.2f} "
                    f"skew_ms={check['time_skew_ms']} "
                    f"action={check['action']}",
                    flush=True,
                )
            except Exception as exc:
                print(f"reference_price_error {type(exc).__name__}: {exc}", flush=True)
            if args.once:
                break
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, args.interval_seconds - elapsed))

    conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--http-timeout", type=float, default=10.0)
    parser.add_argument("--rtds-timeout-seconds", type=float, default=6.0)
    parser.add_argument("--brti-url", default=BRTI_PRICE_URL)
    parser.add_argument("--polymarket-rtds-url", default=POLYMARKET_RTDS_URL)
    parser.add_argument("--polymarket-symbol", default=POLYMARKET_RTDS_SYMBOL)
    parser.add_argument(
        "--mismatch-threshold",
        type=float,
        default=35.0,
        help="Dollar difference that marks the two BTC reference prices as mismatched.",
    )
    parser.add_argument(
        "--stale-after-ms",
        type=int,
        default=1500,
        help="Maximum allowed observation-time skew before pausing trading.",
    )
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
