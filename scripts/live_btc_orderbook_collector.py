"""Live crypto 15-minute Up/Down Polymarket order book collector.

Polls the current market once per second and stores order book snapshots in SQLite.

Example:
  python3 scripts/live_btc_orderbook_collector.py
  python3 scripts/live_btc_orderbook_collector.py --once
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


GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"
BTC_15M_SECONDS = 15 * 60
DEFAULT_SYMBOLS = ("btc", "eth", "sol", "doge", "xrp")
DEFAULT_DB_DIR = Path("data/live_orderbooks")


@dataclass
class CurrentMarket:
    symbol: str
    slug: str
    title: str
    market_id: str
    condition_id: str
    event_start_ts: int
    end_ts: int
    up_token_id: str
    down_token_id: str


def utc_now_ts() -> int:
    return int(time.time())


def utc_now_ms() -> int:
    return int(time.time() * 1000)


def utc_iso(ts: int | None = None) -> str:
    value = utc_now_ts() if ts is None else ts
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def parse_symbols(value: str) -> list[str]:
    return [symbol.strip().lower() for symbol in value.split(",") if symbol.strip()]


def db_path_for_symbol(symbol: str, db_dir: Path = DEFAULT_DB_DIR) -> Path:
    return db_dir / f"{symbol.lower()}_updown_orderbooks.sqlite"


def db_paths_for_symbols(
    symbols: list[str],
    db_dir: Path = DEFAULT_DB_DIR,
    single_db: Path | None = None,
) -> dict[str, Path]:
    if single_db is not None:
        if len(symbols) != 1:
            raise ValueError("--db can only be used with exactly one symbol; use --db-dir for multi-coin collection")
        return {symbols[0]: single_db}
    return {symbol: db_path_for_symbol(symbol, db_dir) for symbol in symbols}


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    return []


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS markets (
            slug TEXT PRIMARY KEY,
            symbol TEXT NOT NULL DEFAULT 'btc',
            title TEXT NOT NULL,
            market_id TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            event_start_ts INTEGER NOT NULL,
            event_start_utc TEXT NOT NULL,
            end_ts INTEGER NOT NULL,
            end_utc TEXT NOT NULL,
            up_token_id TEXT NOT NULL,
            down_token_id TEXT NOT NULL,
            first_seen_ts INTEGER NOT NULL,
            last_seen_ts INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL DEFAULT 'btc',
            slug TEXT NOT NULL,
            token_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            collected_ts INTEGER NOT NULL,
            collected_ms INTEGER,
            collected_utc TEXT NOT NULL,
            book_timestamp TEXT,
            book_hash TEXT,
            best_bid REAL,
            best_ask REAL,
            mid_price REAL,
            spread REAL,
            last_trade_price REAL,
            raw_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_slug_time
            ON orderbook_snapshots(slug, collected_ts);
        CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_token_time
            ON orderbook_snapshots(token_id, collected_ts);

        CREATE TABLE IF NOT EXISTS orderbook_levels (
            snapshot_id INTEGER NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            size REAL NOT NULL,
            level_index INTEGER NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES orderbook_snapshots(id)
        );

        CREATE INDEX IF NOT EXISTS idx_orderbook_levels_snapshot
            ON orderbook_levels(snapshot_id);
        """
    )
    ensure_column(conn, "markets", "symbol", "TEXT NOT NULL DEFAULT 'btc'")
    ensure_column(conn, "orderbook_snapshots", "symbol", "TEXT NOT NULL DEFAULT 'btc'")
    ensure_column(conn, "orderbook_snapshots", "collected_ms", "INTEGER")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_symbol_time
            ON orderbook_snapshots(symbol, collected_ts)
        """
    )
    conn.commit()
    return conn


def ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


def upsert_market(
    conn: sqlite3.Connection,
    market: CurrentMarket,
    commit: bool = True,
) -> None:
    now = utc_now_ts()
    conn.execute(
        """
        INSERT INTO markets (
            slug, symbol, title, market_id, condition_id, event_start_ts, event_start_utc,
            end_ts, end_utc, up_token_id, down_token_id, first_seen_ts, last_seen_ts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            symbol=excluded.symbol,
            title=excluded.title,
            market_id=excluded.market_id,
            condition_id=excluded.condition_id,
            event_start_ts=excluded.event_start_ts,
            event_start_utc=excluded.event_start_utc,
            end_ts=excluded.end_ts,
            end_utc=excluded.end_utc,
            up_token_id=excluded.up_token_id,
            down_token_id=excluded.down_token_id,
            last_seen_ts=excluded.last_seen_ts
        """,
        (
            market.slug,
            market.symbol,
            market.title,
            market.market_id,
            market.condition_id,
            market.event_start_ts,
            utc_iso(market.event_start_ts),
            market.end_ts,
            utc_iso(market.end_ts),
            market.up_token_id,
            market.down_token_id,
            now,
            now,
        ),
    )
    if commit:
        conn.commit()


def price_size(item: dict[str, Any]) -> tuple[float, float]:
    return float(item["price"]), float(item["size"])


def best_bid_ask(book: dict[str, Any]) -> tuple[float | None, float | None]:
    bids = [price_size(item)[0] for item in book.get("bids", [])]
    asks = [price_size(item)[0] for item in book.get("asks", [])]
    best_bid = max(bids) if bids else None
    best_ask = min(asks) if asks else None
    return best_bid, best_ask


def insert_book(
    conn: sqlite3.Connection,
    market: CurrentMarket,
    token_id: str,
    outcome: str,
    book: dict[str, Any],
    collected_ms: int | None = None,
    commit: bool = True,
) -> int:
    collected_ms = utc_now_ms() if collected_ms is None else collected_ms
    collected_ts = collected_ms // 1000
    best_bid, best_ask = best_bid_ask(book)
    mid_price = None
    spread = None
    if best_bid is not None and best_ask is not None:
        mid_price = (best_bid + best_ask) / 2
        spread = best_ask - best_bid

    cursor = conn.execute(
        """
        INSERT INTO orderbook_snapshots (
            symbol, slug, token_id, outcome, collected_ts, collected_ms, collected_utc, book_timestamp,
            book_hash, best_bid, best_ask, mid_price, spread, last_trade_price, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market.symbol,
            market.slug,
            token_id,
            outcome,
            collected_ts,
            collected_ms,
            utc_iso(collected_ts),
            str(book.get("timestamp") or ""),
            str(book.get("hash") or ""),
            best_bid,
            best_ask,
            mid_price,
            spread,
            float(book["last_trade_price"]) if book.get("last_trade_price") else None,
            json.dumps(book, separators=(",", ":")),
        ),
    )
    snapshot_id = int(cursor.lastrowid)

    level_rows = []
    for side in ("bids", "asks"):
        levels = sorted(
            book.get(side, []),
            key=lambda item: float(item["price"]),
            reverse=side == "bids",
        )
        for index, level in enumerate(levels):
            price, size = price_size(level)
            level_rows.append((snapshot_id, side[:-1], price, size, index))

    conn.executemany(
        """
        INSERT INTO orderbook_levels (snapshot_id, side, price, size, level_index)
        VALUES (?, ?, ?, ?, ?)
        """,
        level_rows,
    )
    if commit:
        conn.commit()
    return snapshot_id


async def fetch_event(client: httpx.AsyncClient, slug: str) -> dict[str, Any] | None:
    response = await client.get(f"{GAMMA_BASE_URL}/events/slug/{slug}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def parse_current_market(event: dict[str, Any], symbol: str) -> CurrentMarket | None:
    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    if market.get("closed"):
        return None

    outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
    token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds"))]
    if len(outcomes) != len(token_ids):
        return None
    token_by_outcome = dict(zip(outcomes, token_ids))
    if not token_by_outcome.get("Up") or not token_by_outcome.get("Down"):
        return None

    start = parse_dt(market.get("eventStartTime")) or parse_dt(market.get("startDate"))
    end = parse_dt(market.get("endDate"))
    if not start or not end:
        return None

    now = datetime.now(timezone.utc)
    if not (start <= now <= end):
        return None

    return CurrentMarket(
        symbol=symbol,
        slug=event.get("slug") or market.get("slug") or "",
        title=event.get("title") or market.get("question") or "",
        market_id=str(market.get("id") or ""),
        condition_id=market.get("conditionId") or "",
        event_start_ts=int(start.timestamp()),
        end_ts=int(end.timestamp()),
        up_token_id=token_by_outcome["Up"],
        down_token_id=token_by_outcome["Down"],
    )


async def discover_current_market(
    client: httpx.AsyncClient,
    symbol: str = "btc",
) -> CurrentMarket:
    symbol = symbol.lower()
    current_slot = utc_now_ts() // BTC_15M_SECONDS * BTC_15M_SECONDS
    candidate_slots = [
        current_slot + offset * BTC_15M_SECONDS
        for offset in (0, -1, 1, -2, 2, -3, 3)
    ]
    for slot in candidate_slots:
        slug = f"{symbol}-updown-15m-{slot}"
        event = await fetch_event(client, slug)
        if not event:
            continue
        market = parse_current_market(event, symbol)
        if market:
            return market
    raise RuntimeError(f"No active {symbol.upper()} 15-minute Up/Down market found")


async def fetch_book(client: httpx.AsyncClient, token_id: str) -> dict[str, Any]:
    response = await client.get(f"{CLOB_BASE_URL}/book", params={"token_id": token_id})
    response.raise_for_status()
    return response.json()


async def fetch_books(client: httpx.AsyncClient, token_ids: list[str]) -> list[dict[str, Any]]:
    response = await client.post(
        f"{CLOB_BASE_URL}/books",
        json=[{"token_id": token_id} for token_id in token_ids],
    )
    response.raise_for_status()
    return response.json()


async def poll_once(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    market: CurrentMarket,
) -> None:
    up_book, down_book = await asyncio.gather(
        fetch_book(client, market.up_token_id),
        fetch_book(client, market.down_token_id),
    )
    collected_ms = utc_now_ms()
    upsert_market(conn, market, commit=False)
    insert_book(conn, market, market.up_token_id, "Up", up_book, collected_ms, commit=False)
    insert_book(
        conn, market, market.down_token_id, "Down", down_book, collected_ms, commit=False
    )
    conn.commit()

    up_bid, up_ask = best_bid_ask(up_book)
    down_bid, down_ask = best_bid_ask(down_book)
    print(
        f"{utc_iso()} {market.slug} "
        f"Up bid/ask={up_bid}/{up_ask} Down bid/ask={down_bid}/{down_ask}",
        flush=True,
    )


async def poll_markets_once(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    markets: list[CurrentMarket],
) -> None:
    token_ids = []
    for market in markets:
        token_ids.append(market.up_token_id)
        token_ids.append(market.down_token_id)
    books = await fetch_books(client, token_ids)
    books_by_token = {
        str(book.get("asset_id") or book.get("token_id") or ""): book for book in books
    }
    collected_ms = utc_now_ms()

    paired_books: list[tuple[CurrentMarket, dict[str, Any], dict[str, Any]]] = []
    for market in markets:
        up_book = books_by_token[market.up_token_id]
        down_book = books_by_token[market.down_token_id]
        paired_books.append((market, up_book, down_book))
        upsert_market(conn, market, commit=False)
        insert_book(
            conn, market, market.up_token_id, "Up", up_book, collected_ms, commit=False
        )
        insert_book(
            conn,
            market,
            market.down_token_id,
            "Down",
            down_book,
            collected_ms,
            commit=False,
        )
    conn.commit()

    for market, up_book, down_book in paired_books:
        up_bid, up_ask = best_bid_ask(up_book)
        down_bid, down_ask = best_bid_ask(down_book)
        print(
            f"{utc_iso(collected_ms // 1000)} {market.slug} "
            f"Up bid/ask={up_bid}/{up_ask} Down bid/ask={down_bid}/{down_ask}",
            flush=True,
        )


async def run(args: argparse.Namespace) -> None:
    stop = asyncio.Event()

    def handle_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_stop)

    current_markets: dict[str, CurrentMarket] = {}
    symbols = parse_symbols(args.symbols)
    if not symbols:
        raise ValueError("At least one symbol is required")
    db_paths = db_paths_for_symbols(symbols, args.db_dir, args.db)
    connections = {symbol: init_db(path) for symbol, path in db_paths.items()}
    for symbol, path in db_paths.items():
        print(f"writing {symbol.upper()} snapshots to {path}", flush=True)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            while not stop.is_set():
                now = utc_now_ts()
                for symbol in symbols:
                    current_market = current_markets.get(symbol)
                    if (
                        current_market is None
                        or now >= current_market.end_ts
                        or now < current_market.event_start_ts
                    ):
                        current_market = await discover_current_market(client, symbol)
                        current_markets[symbol] = current_market
                        print(
                            f"tracking {current_market.symbol.upper()} {current_market.slug} "
                            f"{utc_iso(current_market.event_start_ts)}..{utc_iso(current_market.end_ts)}",
                            flush=True,
                        )

                started = time.monotonic()
                await asyncio.gather(
                    *[
                        poll_once(client, connections[symbol], current_markets[symbol])
                        for symbol in symbols
                        if symbol in current_markets
                    ]
                )
                if args.once:
                    break
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, args.interval_seconds - elapsed))
    finally:
        for conn in connections.values():
            conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Single-symbol SQLite output path. For multiple symbols, use --db-dir.",
    )
    parser.add_argument(
        "--db-dir",
        type=Path,
        default=DEFAULT_DB_DIR,
        help="Directory for per-symbol SQLite files like btc_updown_orderbooks.sqlite.",
    )
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated symbols to collect, e.g. btc,eth,sol,doge,xrp.",
    )
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
