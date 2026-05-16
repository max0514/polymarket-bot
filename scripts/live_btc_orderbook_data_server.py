"""24/7 crypto 15-minute Up/Down Polymarket order book data server.

This process does two jobs:
  1. Collects active crypto 15m Up/Down order books every second into SQLite.
  2. Serves JSON endpoints for downstream bots, dashboards, or research jobs.

Run:
  python3 scripts/live_btc_orderbook_data_server.py --host 0.0.0.0 --port 8765

Endpoints:
  GET /health
  GET /api/state
  GET /api/latest
  GET /api/snapshots?limit=200
  GET /api/levels?snapshot_id=123
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import live_btc_orderbook_collector as collector  # noqa: E402
from live_btc_orderbook_collector import (  # noqa: E402
    CurrentMarket,
    DEFAULT_SYMBOLS,
    discover_current_market,
    init_db,
    poll_markets_once,
    utc_iso,
    utc_now_ts,
)
from live_orderbook_dashboard import load_state  # noqa: E402


DEFAULT_DB = Path("data/live_orderbooks/btc_updown_orderbooks.sqlite")


class ServerState:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.started_ts = utc_now_ts()
        self.last_collected_ts: int | None = None
        self.last_error: str | None = None
        self.current_slugs: dict[str, str] = {}
        self.lock = threading.Lock()

    def set_collection(self, market: CurrentMarket) -> None:
        with self.lock:
            self.last_collected_ts = utc_now_ts()
            self.last_error = None
            self.current_slugs[market.symbol] = market.slug

    def set_error(self, error: Exception) -> None:
        with self.lock:
            self.last_error = f"{type(error).__name__}: {error}"

    def snapshot(self) -> dict:
        with self.lock:
            last_collected_ts = self.last_collected_ts
            return {
                "started_ts": self.started_ts,
                "started_utc": utc_iso(self.started_ts),
                "uptime_seconds": utc_now_ts() - self.started_ts,
                "last_collected_ts": last_collected_ts,
                "last_collected_utc": utc_iso(last_collected_ts)
                if last_collected_ts
                else None,
                "last_collection_age_seconds": utc_now_ts() - last_collected_ts
                if last_collected_ts
                else None,
                "last_error": self.last_error,
                "current_slugs": self.current_slugs,
            }


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def latest_snapshots(db_path: Path) -> dict:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM orderbook_snapshots
            WHERE id IN (
                SELECT MAX(id)
                FROM orderbook_snapshots
                GROUP BY symbol, outcome
            )
            ORDER BY symbol ASC, outcome DESC
            """
        ).fetchall()
    payload: dict[str, dict] = {}
    for row in rows:
        payload.setdefault(row["symbol"], {})[row["outcome"]] = dict(row)
    return payload


def query_snapshots(db_path: Path, params: dict[str, list[str]]) -> list[dict]:
    limit = min(1000, max(1, int(params.get("limit", ["200"])[0])))
    symbol = params.get("symbol", [None])[0]
    slug = params.get("slug", [None])[0]
    outcome = params.get("outcome", [None])[0]

    clauses = []
    values: list[object] = []
    if symbol:
        clauses.append("symbol = ?")
        values.append(symbol.lower())
    if slug:
        clauses.append("slug = ?")
        values.append(slug)
    if outcome:
        clauses.append("outcome = ?")
        values.append(outcome)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""

    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT id, symbol, slug, token_id, outcome, collected_ts, collected_utc,
                   best_bid, best_ask, mid_price, spread, last_trade_price
            FROM orderbook_snapshots
            {where}
            ORDER BY collected_ts DESC, id DESC
            LIMIT ?
            """,
            (*values, limit),
        ).fetchall()
    return rows_to_dicts(rows)


def query_levels(db_path: Path, params: dict[str, list[str]]) -> list[dict]:
    snapshot_id = int(params.get("snapshot_id", ["0"])[0])
    if snapshot_id <= 0:
        return []
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT snapshot_id, side, price, size, level_index
            FROM orderbook_levels
            WHERE snapshot_id = ?
            ORDER BY side, level_index
            """,
            (snapshot_id,),
        ).fetchall()
    return rows_to_dicts(rows)


class DataServerHandler(BaseHTTPRequestHandler):
    db_path: Path
    state: ServerState

    def log_message(self, format: str, *args) -> None:
        return

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self.send_json(
                    {
                        "service": "Polymarket crypto 15m order book data server",
                        "endpoints": [
                            "/health",
                            "/api/state",
                            "/api/latest",
                            "/api/snapshots?limit=200",
                            "/api/snapshots?symbol=eth&limit=200",
                            "/api/snapshots?outcome=Up&limit=200",
                            "/api/levels?snapshot_id=123",
                        ],
                    }
                )
                return
            if parsed.path == "/health":
                service = self.state.snapshot()
                age = service["last_collection_age_seconds"]
                healthy = age is not None and age <= 10 and not service["last_error"]
                self.send_json(
                    {
                        "ok": healthy,
                        "service": service,
                        "database_exists": self.db_path.exists(),
                    },
                    HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            if parsed.path == "/api/state":
                payload = load_state(self.db_path)
                payload["service"] = self.state.snapshot()
                self.send_json(payload)
                return
            if parsed.path == "/api/latest":
                self.send_json(latest_snapshots(self.db_path))
                return
            if parsed.path == "/api/snapshots":
                self.send_json(query_snapshots(self.db_path, params))
                return
            if parsed.path == "/api/levels":
                self.send_json(query_levels(self.db_path, params))
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json(
                {"error": f"{type(exc).__name__}: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def start_http_server(
    host: str,
    port: int,
    db_path: Path,
    state: ServerState,
) -> ThreadingHTTPServer:
    DataServerHandler.db_path = db_path
    DataServerHandler.state = state
    server = ThreadingHTTPServer((host, port), DataServerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Data server listening on http://{host}:{port}", flush=True)
    return server


async def collector_loop(args: argparse.Namespace, state: ServerState) -> None:
    conn = init_db(args.db)
    current_markets: dict[str, CurrentMarket] = {}
    symbols = [symbol.strip().lower() for symbol in args.symbols.split(",") if symbol.strip()]
    backoff = 1.0

    async with httpx.AsyncClient(timeout=args.http_timeout, verify=not args.no_verify_tls) as client:
        while True:
            try:
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
                            f"{utc_iso(current_market.event_start_ts)}.."
                            f"{utc_iso(current_market.end_ts)}",
                            flush=True,
                        )

                started = time.monotonic()
                await poll_markets_once(client, conn, list(current_markets.values()))
                for market in current_markets.values():
                    state.set_collection(market)
                backoff = 1.0
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, args.interval_seconds - elapsed))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.set_error(exc)
                print(f"collector_error {type(exc).__name__}: {exc}", flush=True)
                current_markets = {}
                await asyncio.sleep(backoff)
                backoff = min(args.max_backoff_seconds, backoff * 2)


async def run(args: argparse.Namespace) -> None:
    collector.GAMMA_BASE_URL = args.gamma_base_url.rstrip("/")
    collector.CLOB_BASE_URL = args.clob_base_url.rstrip("/")
    state = ServerState(args.db)
    server = start_http_server(args.host, args.port, args.db, state)
    stop = asyncio.Event()

    def request_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)

    task = asyncio.create_task(collector_loop(args, state))
    await stop.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    server.shutdown()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated symbols to collect, e.g. btc,eth,sol,doge,xrp.",
    )
    parser.add_argument("--http-timeout", type=float, default=10.0)
    parser.add_argument("--max-backoff-seconds", type=float, default=30.0)
    parser.add_argument("--gamma-base-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--clob-base-url", default="https://clob.polymarket.com")
    parser.add_argument(
        "--no-verify-tls",
        action="store_true",
        help="Disable TLS verification if local network/cert interception breaks API calls.",
    )
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
