"""Live Kalshi BTC 15-minute Up/Down order book collector.

Polls the active KXBTC15M market and stores order book snapshots in SQLite.

Examples:
  python3 scripts/live_kalshi_btc15_orderbook_collector.py
  python3 scripts/live_kalshi_btc15_orderbook_collector.py --once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


KALSHI_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_SERIES_TICKER = "KXBTC15M"
DEFAULT_DB = Path("data/live_orderbooks/kalshi_btc15_orderbooks.sqlite")


@dataclass
class KalshiMarket:
    series_ticker: str
    ticker: str
    event_ticker: str
    title: str
    open_ts: int
    close_ts: int
    yes_sub_title: str
    no_sub_title: str
    floor_strike: float | None
    raw_market: dict[str, Any]


def utc_now_ts() -> int:
    return int(time.time())


def utc_now_ms() -> int:
    return int(time.time() * 1000)


def utc_iso(ts: int | None = None) -> str:
    value = utc_now_ts() if ts is None else ts
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS markets (
            ticker TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT 'kalshi',
            series_ticker TEXT NOT NULL,
            event_ticker TEXT NOT NULL,
            title TEXT NOT NULL,
            open_ts INTEGER NOT NULL,
            open_utc TEXT NOT NULL,
            close_ts INTEGER NOT NULL,
            close_utc TEXT NOT NULL,
            yes_sub_title TEXT,
            no_sub_title TEXT,
            floor_strike REAL,
            raw_json TEXT NOT NULL,
            first_seen_ts INTEGER NOT NULL,
            last_seen_ts INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL DEFAULT 'kalshi',
            symbol TEXT NOT NULL DEFAULT 'btc',
            ticker TEXT NOT NULL,
            token_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            collected_ts INTEGER NOT NULL,
            collected_ms INTEGER NOT NULL,
            collected_utc TEXT NOT NULL,
            best_bid REAL,
            best_ask REAL,
            mid_price REAL,
            spread REAL,
            last_trade_price REAL,
            raw_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_kalshi_snapshots_ticker_time
            ON orderbook_snapshots(ticker, collected_ts);
        CREATE INDEX IF NOT EXISTS idx_kalshi_snapshots_outcome_time
            ON orderbook_snapshots(outcome, collected_ts);

        CREATE TABLE IF NOT EXISTS orderbook_levels (
            snapshot_id INTEGER NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            size REAL NOT NULL,
            level_index INTEGER NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES orderbook_snapshots(id)
        );

        CREATE INDEX IF NOT EXISTS idx_kalshi_levels_snapshot
            ON orderbook_levels(snapshot_id);
        """
    )
    conn.commit()
    return conn


def price_size(level: list[Any]) -> tuple[float, float]:
    return float(level[0]), float(level[1])


def best_bid(levels: list[list[Any]]) -> float | None:
    return max((price_size(level)[0] for level in levels), default=None)


def derived_asks(opposite_bids: list[list[Any]]) -> list[tuple[float, float]]:
    asks = [(1.0 - price, size) for price, size in map(price_size, opposite_bids)]
    return sorted(asks, key=lambda item: item[0])


def normalize_levels(levels: list[list[Any]], reverse: bool) -> list[tuple[float, float]]:
    normalized = [price_size(level) for level in levels]
    return sorted(normalized, key=lambda item: item[0], reverse=reverse)


def parse_market(raw: dict[str, Any], series_ticker: str) -> KalshiMarket | None:
    ticker = str(raw.get("ticker") or "")
    open_dt = parse_dt(raw.get("open_time"))
    close_dt = parse_dt(raw.get("close_time"))
    if not ticker or not open_dt or not close_dt:
        return None

    return KalshiMarket(
        series_ticker=series_ticker,
        ticker=ticker,
        event_ticker=str(raw.get("event_ticker") or ""),
        title=str(raw.get("title") or ""),
        open_ts=int(open_dt.timestamp()),
        close_ts=int(close_dt.timestamp()),
        yes_sub_title=str(raw.get("yes_sub_title") or ""),
        no_sub_title=str(raw.get("no_sub_title") or ""),
        floor_strike=float(raw["floor_strike"]) if raw.get("floor_strike") is not None else None,
        raw_market=raw,
    )


async def discover_current_market(
    client: httpx.AsyncClient,
    series_ticker: str = DEFAULT_SERIES_TICKER,
) -> KalshiMarket:
    response = await client.get(
        f"{KALSHI_BASE_URL}/markets",
        params={"series_ticker": series_ticker, "status": "open", "limit": 10},
    )
    response.raise_for_status()
    markets = response.json().get("markets") or []
    now = utc_now_ts()
    parsed = [
        market
        for market in (parse_market(raw, series_ticker) for raw in markets)
        if market and market.open_ts <= now <= market.close_ts
    ]
    if not parsed:
        raise RuntimeError(f"No active Kalshi {series_ticker} market found")
    return min(parsed, key=lambda market: market.close_ts)


async def fetch_orderbook(client: httpx.AsyncClient, ticker: str) -> dict[str, Any]:
    response = await client.get(f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook")
    response.raise_for_status()
    return response.json()


def upsert_market(
    conn: sqlite3.Connection,
    market: KalshiMarket,
    commit: bool = True,
) -> None:
    now = utc_now_ts()
    conn.execute(
        """
        INSERT INTO markets (
            ticker, source, series_ticker, event_ticker, title, open_ts, open_utc,
            close_ts, close_utc, yes_sub_title, no_sub_title, floor_strike,
            raw_json, first_seen_ts, last_seen_ts
        )
        VALUES (?, 'kalshi', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            event_ticker=excluded.event_ticker,
            title=excluded.title,
            open_ts=excluded.open_ts,
            open_utc=excluded.open_utc,
            close_ts=excluded.close_ts,
            close_utc=excluded.close_utc,
            yes_sub_title=excluded.yes_sub_title,
            no_sub_title=excluded.no_sub_title,
            floor_strike=excluded.floor_strike,
            raw_json=excluded.raw_json,
            last_seen_ts=excluded.last_seen_ts
        """,
        (
            market.ticker,
            market.series_ticker,
            market.event_ticker,
            market.title,
            market.open_ts,
            utc_iso(market.open_ts),
            market.close_ts,
            utc_iso(market.close_ts),
            market.yes_sub_title,
            market.no_sub_title,
            market.floor_strike,
            json.dumps(market.raw_market, separators=(",", ":")),
            now,
            now,
        ),
    )
    if commit:
        conn.commit()


def insert_side(
    conn: sqlite3.Connection,
    market: KalshiMarket,
    outcome: str,
    bid_levels: list[list[Any]],
    ask_levels: list[tuple[float, float]],
    raw_book: dict[str, Any],
    collected_ms: int,
    commit: bool = True,
) -> int:
    best_bid_price = best_bid(bid_levels)
    best_ask_price = ask_levels[0][0] if ask_levels else None
    mid_price = None
    spread = None
    if best_bid_price is not None and best_ask_price is not None:
        mid_price = (best_bid_price + best_ask_price) / 2
        spread = best_ask_price - best_bid_price

    cursor = conn.execute(
        """
        INSERT INTO orderbook_snapshots (
            source, symbol, ticker, token_id, outcome, collected_ts, collected_ms,
            collected_utc, best_bid, best_ask, mid_price, spread, last_trade_price,
            raw_json
        )
        VALUES ('kalshi', 'btc', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market.ticker,
            f"{market.ticker}:{outcome.lower()}",
            outcome,
            collected_ms // 1000,
            collected_ms,
            utc_iso(collected_ms // 1000),
            best_bid_price,
            best_ask_price,
            mid_price,
            spread,
            float(market.raw_market["last_price_dollars"])
            if market.raw_market.get("last_price_dollars")
            else None,
            json.dumps(raw_book, separators=(",", ":")),
        ),
    )
    snapshot_id = int(cursor.lastrowid)

    rows = []
    for index, (price, size) in enumerate(normalize_levels(bid_levels, reverse=True)):
        rows.append((snapshot_id, "bid", price, size, index))
    for index, (price, size) in enumerate(ask_levels):
        rows.append((snapshot_id, "ask", price, size, index))

    conn.executemany(
        """
        INSERT INTO orderbook_levels (snapshot_id, side, price, size, level_index)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )
    if commit:
        conn.commit()
    return snapshot_id


async def poll_once(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    market: KalshiMarket,
) -> None:
    raw_book = await fetch_orderbook(client, market.ticker)
    orderbook = raw_book.get("orderbook_fp") or raw_book.get("orderbook") or {}
    yes_bids = orderbook.get("yes_dollars") or orderbook.get("yes") or []
    no_bids = orderbook.get("no_dollars") or orderbook.get("no") or []
    collected_ms = utc_now_ms()

    upsert_market(conn, market, commit=False)
    insert_side(
        conn,
        market,
        "Up",
        yes_bids,
        derived_asks(no_bids),
        raw_book,
        collected_ms,
        commit=False,
    )
    insert_side(
        conn,
        market,
        "Down",
        no_bids,
        derived_asks(yes_bids),
        raw_book,
        collected_ms,
        commit=False,
    )
    conn.commit()

    up_bid = best_bid(yes_bids)
    down_bid = best_bid(no_bids)
    up_ask = (1.0 - down_bid) if down_bid is not None else None
    down_ask = (1.0 - up_bid) if up_bid is not None else None
    print(
        f"{utc_iso(collected_ms // 1000)} {market.ticker} "
        f"Up bid/ask={up_bid}/{up_ask} Down bid/ask={down_bid}/{down_ask}",
        flush=True,
    )


async def run(args: argparse.Namespace) -> None:
    conn = init_db(args.db)
    stop = asyncio.Event()

    def handle_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_stop)

    current_market: KalshiMarket | None = None
    print(f"writing Kalshi BTC15 snapshots to {args.db}", flush=True)

    try:
        async with httpx.AsyncClient(timeout=args.http_timeout) as client:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    now = utc_now_ts()
                    if current_market is None or now >= current_market.close_ts or now < current_market.open_ts:
                        current_market = await discover_current_market(client, args.series_ticker)
                        print(
                            f"tracking {current_market.series_ticker} {current_market.ticker} "
                            f"{utc_iso(current_market.open_ts)}..{utc_iso(current_market.close_ts)}",
                            flush=True,
                        )

                    await poll_once(client, conn, current_market)
                    if args.once:
                        break
                except (httpx.HTTPError, RuntimeError) as exc:
                    current_market = None
                    print(
                        f"{utc_iso()} Kalshi collector warning: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    if args.once:
                        raise
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, args.interval_seconds - elapsed))
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--series-ticker", default=DEFAULT_SERIES_TICKER)
    parser.add_argument("--interval-seconds", type=float, default=0.25)
    parser.add_argument("--http-timeout", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
