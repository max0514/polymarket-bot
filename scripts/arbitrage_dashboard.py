"""Web dashboard for Kalshi vs Polymarket BTC 15m arbitrage.

Reads live order book SQLite files, computes executable cross-venue arbitrage,
records detected opportunities, and serves a local dashboard.

Run:
  python3 scripts/arbitrage_dashboard.py --port 8770
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_POLY_DB = Path("data/live_orderbooks/crypto_updown_orderbooks.sqlite")
DEFAULT_KALSHI_DB = Path("data/live_orderbooks/kalshi_btc15_orderbooks.sqlite")
DEFAULT_ARB_DB = Path("data/live_orderbooks/kalshi_polymarket_arbitrage.sqlite")
DEFAULT_REFERENCE_DB = Path("data/live_orderbooks/btc_reference_prices.sqlite")


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BTC 15m Arbitrage</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #080b0f;
      --panel: #111820;
      --panel2: #17212c;
      --line: #263341;
      --text: #edf7f4;
      --muted: #8fa1ad;
      --green: #1fe08b;
      --red: #ff5f6d;
      --amber: #ffc857;
      --blue: #6bb7ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at 15% -10%, rgba(31,224,139,.18), transparent 35%),
        radial-gradient(circle at 90% 0%, rgba(107,183,255,.16), transparent 34%),
        linear-gradient(180deg, #0b1016, var(--bg));
      color: var(--text);
      font-family: "Avenir Next", "Trebuchet MS", ui-sans-serif, system-ui, sans-serif;
    }
    header {
      padding: 22px clamp(16px, 3vw, 34px);
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 18px;
      border-bottom: 1px solid rgba(255,255,255,.08);
      backdrop-filter: blur(14px);
      position: sticky;
      top: 0;
      z-index: 2;
      background: rgba(8,11,15,.75);
    }
    h1 { margin: 0; font-size: clamp(24px, 4vw, 44px); letter-spacing: -.05em; }
    .sub { color: var(--muted); margin-top: 6px; font-size: 13px; }
    .status { color: var(--muted); font-size: 13px; text-align: right; }
    main { width: min(1500px, 100%); margin: 0 auto; padding: 18px; }
    .cards { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }
    .card, .panel {
      background: linear-gradient(180deg, rgba(23,33,44,.96), rgba(14,20,28,.96));
      border: 1px solid rgba(255,255,255,.09);
      box-shadow: 0 18px 60px rgba(0,0,0,.24);
      border-radius: 18px;
    }
    .card { padding: 16px; min-height: 105px; }
    .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
    .value { margin-top: 10px; font-size: clamp(22px, 3vw, 34px); font-weight: 800; letter-spacing: -.04em; }
    .small { font-size: 13px; line-height: 1.45; color: var(--muted); }
    .green { color: var(--green); }
    .red { color: var(--red); }
    .amber { color: var(--amber); }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 14px; }
    .panel { overflow: hidden; }
    .panel h2 { margin: 0; padding: 15px 16px; border-bottom: 1px solid var(--line); font-size: 15px; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td {
      padding: 9px 11px;
      border-bottom: 1px solid rgba(255,255,255,.055);
      text-align: right;
      font-size: 12px;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    th { color: var(--muted); font-weight: 700; background: rgba(255,255,255,.035); }
    th:first-child, td:first-child { text-align: left; }
    .wide { grid-column: 1 / -1; }
    .pill { display:inline-block; padding: 4px 8px; border-radius: 999px; background: rgba(31,224,139,.13); color: var(--green); }
    .empty { padding: 22px; color: var(--muted); }
    canvas {
      width: 100%;
      height: 280px;
      display: block;
      background: rgba(0,0,0,.16);
      border-bottom: 1px solid rgba(255,255,255,.055);
    }
    .legend {
      display: flex;
      gap: 14px;
      padding: 10px 14px;
      color: var(--muted);
      font-size: 12px;
    }
    .swatch { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 6px; }
    @media (max-width: 1050px) {
      .cards, .grid { grid-template-columns: 1fr; }
      header { align-items: start; flex-direction: column; }
      .status { text-align: left; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>BTC 15m Arbitrage</h1>
      <div class="sub">Kalshi KXBTC15M vs Polymarket BTC Up/Down. The PnL shown is theoretical executable edge before fees, latency, failed fills, and resolution mismatch risk.</div>
    </div>
    <div class="status" id="status">connecting</div>
  </header>
  <main>
    <section class="cards">
      <div class="card"><div class="label">Best Edge</div><div id="bestEdge" class="value">-</div></div>
      <div class="card"><div class="label">Max Size</div><div id="maxSize" class="value">-</div></div>
      <div class="card"><div class="label">Max Net Profit</div><div id="maxProfit" class="value">-</div></div>
      <div class="card"><div class="label">Cumulative Net Recorded</div><div id="cumProfit" class="value">-</div></div>
      <div class="card"><div class="label">Reference Risk</div><div id="referenceRisk" class="value">-</div></div>
    </section>
    <section class="panel wide" id="warningsPanel" style="display:none; margin-top:14px;">
      <h2>Live Data Warnings</h2>
      <div class="empty" id="warnings"></div>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Current Prices</h2>
        <table id="quotes"></table>
      </div>
      <div class="panel">
        <h2>Best Arbitrage Now</h2>
        <table id="opps"></table>
      </div>
      <div class="panel wide">
        <h2>Kalshi - Polymarket Price Difference</h2>
        <canvas id="diffChart" width="1400" height="320"></canvas>
        <div class="legend">
          <span><span class="swatch" style="background: var(--green)"></span>Up mid diff</span>
          <span><span class="swatch" style="background: var(--red)"></span>Down mid diff</span>
          <span>Positive means Kalshi is higher than Polymarket</span>
        </div>
      </div>
      <div class="panel wide">
        <h2>Recorded Arbitrage Opportunities</h2>
        <table id="records"></table>
      </div>
    </section>
  </main>
  <script>
    const fmt = (x, d=4) => x === null || x === undefined ? "-" : Number(x).toFixed(d);
    const money = (x) => x === null || x === undefined ? "-" : "$" + Number(x).toFixed(4);
    const cls = (x) => Number(x || 0) > 0 ? "green" : "";

    function renderWarnings(rows) {
      const panel = document.getElementById("warningsPanel");
      if (!rows || !rows.length) {
        panel.style.display = "none";
        document.getElementById("warnings").textContent = "";
        return;
      }
      panel.style.display = "block";
      document.getElementById("warnings").innerHTML = rows.map(row => `<div>${row}</div>`).join("");
    }

    function renderReferenceRisk(risk) {
      const el = document.getElementById("referenceRisk");
      if (!risk || risk.status === "missing") {
        el.textContent = "MISSING";
        el.className = "value red";
        return;
      }
      el.textContent = risk.label || risk.action || "UNKNOWN";
      el.className = risk.is_risky ? "value red" : "value green";
    }

    function riskText(row) {
      if (!row.risk_label) return "-";
      if (Number(row.is_risky || 0)) return `${row.risk_label}: ${row.risk_action || "risk"}`;
      return row.risk_label;
    }

    function renderQuotes(rows) {
      const body = rows.map(r => `<tr>
        <td>${r.venue}</td><td>${r.outcome}</td><td>${fmt(r.best_bid)}</td><td>${fmt(r.best_ask)}</td>
        <td>${r.market}</td><td>${r.collected_utc ? r.collected_utc.slice(11,19) : "-"}</td>
      </tr>`).join("");
      document.getElementById("quotes").innerHTML = `<thead><tr>
        <th>Venue</th><th>Outcome</th><th>Bid</th><th>Ask</th><th>Market</th><th>UTC</th>
      </tr></thead><tbody>${body}</tbody>`;
    }

    function renderOpps(rows) {
      if (!rows.length) {
        document.getElementById("opps").innerHTML = `<div class="empty">No executable arbitrage above threshold.</div>`;
        return;
      }
      const body = rows.map(o => `<tr>
        <td><span class="pill">${o.kind}</span></td><td>${o.outcome}</td><td>${o.buy_venue}</td><td>${o.sell_venue || "-"}</td>
        <td class="${cls(o.edge)}">${fmt(o.edge)}</td><td>${fmt(o.max_size, 2)}</td><td class="${cls(o.net_profit)}">${money(o.net_profit)}</td>
        <td class="${Number(o.is_risky || 0) ? "red" : "green"}">${riskText(o)}</td>
      </tr>`).join("");
      document.getElementById("opps").innerHTML = `<thead><tr>
        <th>Type</th><th>Leg</th><th>Buy</th><th>Sell</th><th>Edge</th><th>Max Size</th><th>Net Profit</th><th>Risk</th>
      </tr></thead><tbody>${body}</tbody>`;
    }

    function renderRecords(rows) {
      const body = rows.map(r => `<tr>
        <td>${r.detected_utc.slice(11,19)}</td><td>${r.kind}</td><td>${r.outcome}</td><td>${r.buy_venue}</td><td>${r.sell_venue || "-"}</td>
        <td>${fmt(r.edge)}</td><td>${fmt(r.max_size, 2)}</td><td>${money(r.net_profit)}</td>
        <td class="${Number(r.is_risky || 0) ? "red" : "green"}">${riskText(r)}</td>
      </tr>`).join("");
      document.getElementById("records").innerHTML = `<thead><tr>
        <th>UTC</th><th>Type</th><th>Leg</th><th>Buy</th><th>Sell</th><th>Edge</th><th>Size</th><th>Net Profit</th><th>Risk</th>
      </tr></thead><tbody>${body}</tbody>`;
    }

    function renderDiffChart(rows) {
      const canvas = document.getElementById("diffChart");
      const ctx = canvas.getContext("2d");
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = "rgba(0,0,0,.16)";
      ctx.fillRect(0, 0, w, h);
      const padL = 54, padR = 18, padT = 18, padB = 34;
      ctx.strokeStyle = "rgba(255,255,255,.12)";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const y = padT + i * (h - padT - padB) / 4;
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      }
      if (!rows.length) {
        ctx.fillStyle = "#8fa1ad";
        ctx.font = "15px system-ui";
        ctx.fillText("Waiting for aligned price history", padL, h / 2);
        return;
      }
      const xs = rows.map(r => Number(r.ts));
      const values = rows.flatMap(r => [r.up_diff, r.down_diff]).filter(v => v !== null && v !== undefined).map(Number);
      const minX = Math.min(...xs), maxX = Math.max(...xs);
      const maxAbs = Math.max(0.01, ...values.map(v => Math.abs(v)));
      const xPos = ts => padL + ((ts - minX) / Math.max(1, maxX - minX)) * (w - padL - padR);
      const yPos = value => padT + (maxAbs - Number(value)) / (2 * maxAbs) * (h - padT - padB);
      const zeroY = yPos(0);
      ctx.strokeStyle = "rgba(255,255,255,.34)";
      ctx.beginPath(); ctx.moveTo(padL, zeroY); ctx.lineTo(w - padR, zeroY); ctx.stroke();

      function line(key, color) {
        ctx.strokeStyle = color; ctx.lineWidth = 2.5; ctx.beginPath();
        let started = false;
        for (const row of rows) {
          if (row[key] === null || row[key] === undefined) continue;
          const x = xPos(Number(row.ts)), y = yPos(Number(row[key]));
          if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
        }
        ctx.stroke();
      }
      line("up_diff", "#1fe08b");
      line("down_diff", "#ff5f6d");
      ctx.fillStyle = "#8fa1ad";
      ctx.font = "12px system-ui";
      ctx.fillText("+" + fmt(maxAbs, 3), 8, padT + 4);
      ctx.fillText("0", 30, zeroY + 4);
      ctx.fillText("-" + fmt(maxAbs, 3), 8, h - padB + 4);
      const last = rows[rows.length - 1];
      ctx.fillText(`Latest Up ${fmt(last.up_diff)} | Down ${fmt(last.down_diff)}`, padL, h - 10);
    }

    async function tick() {
      try {
        const res = await fetch("/api/state", {cache: "no-store"});
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        document.getElementById("status").textContent = `live | ${data.latest_time_utc || "-"}`;
        const best = data.best_opportunity;
        document.getElementById("bestEdge").textContent = best ? fmt(best.edge) : "0.0000";
        document.getElementById("bestEdge").className = best ? "value green" : "value";
        document.getElementById("maxSize").textContent = best ? fmt(best.max_size, 2) : "-";
        document.getElementById("maxProfit").textContent = best ? money(best.net_profit) : "$0.0000";
        document.getElementById("maxProfit").className = best ? "value green" : "value";
        document.getElementById("cumProfit").textContent = money(data.cumulative_net_profit);
        renderReferenceRisk(data.reference_risk);
        renderQuotes(data.quotes);
        renderOpps(data.opportunities);
        renderDiffChart(data.price_diff_history || []);
        renderRecords(data.recent_records);
        renderWarnings(data.warnings || []);
      } catch (err) {
        document.getElementById("status").textContent = err.message;
      }
    }
    tick();
    setInterval(tick, 1000);
  </script>
</body>
</html>
"""


@dataclass
class MarketInfo:
    venue: str
    market: str
    start_ts: int
    end_ts: int


@dataclass
class ReferenceRisk:
    status: str
    label: str
    is_risky: bool
    action: str
    checked_utc: str | None = None
    age_seconds: float | None = None
    kalshi_price: float | None = None
    polymarket_price: float | None = None
    price_diff: float | None = None
    abs_price_diff: float | None = None
    time_skew_ms: int | None = None
    mismatch_threshold: float | None = None
    stale_after_ms: int | None = None


@dataclass
class Snapshot:
    venue: str
    market: str
    outcome: str
    snapshot_id: int
    start_ts: int
    end_ts: int
    collected_ts: int
    collected_utc: str
    best_bid: float | None
    best_ask: float | None
    bids: list[dict]
    asks: list[dict]


def utc_iso(ts: int | None = None) -> str:
    value = int(datetime.now(timezone.utc).timestamp()) if ts is None else ts
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_arb_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS arbitrage_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_ts INTEGER NOT NULL,
                detected_utc TEXT NOT NULL,
                kind TEXT NOT NULL,
                outcome TEXT NOT NULL,
                buy_venue TEXT NOT NULL,
                sell_venue TEXT,
                buy_market TEXT NOT NULL,
                sell_market TEXT,
                edge REAL NOT NULL,
                max_size REAL NOT NULL,
                max_profit REAL NOT NULL,
                profit_haircut REAL NOT NULL DEFAULT 0.05,
                net_profit REAL NOT NULL DEFAULT 0,
                risk_label TEXT NOT NULL DEFAULT 'UNKNOWN',
                is_risky INTEGER NOT NULL DEFAULT 0,
                risk_action TEXT NOT NULL DEFAULT 'unknown',
                buy_snapshot_id INTEGER NOT NULL,
                sell_snapshot_id INTEGER,
                legs_json TEXT NOT NULL,
                orderbooks_json TEXT NOT NULL,
                signature TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_arb_detected_ts
                ON arbitrage_opportunities(detected_ts);
            """
        )
        ensure_column(conn, "arbitrage_opportunities", "profit_haircut", "REAL NOT NULL DEFAULT 0.05")
        ensure_column(conn, "arbitrage_opportunities", "net_profit", "REAL NOT NULL DEFAULT 0")
        ensure_column(conn, "arbitrage_opportunities", "risk_label", "TEXT NOT NULL DEFAULT 'UNKNOWN'")
        ensure_column(conn, "arbitrage_opportunities", "is_risky", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "arbitrage_opportunities", "risk_action", "TEXT NOT NULL DEFAULT 'unknown'")
        conn.execute(
            """
            UPDATE arbitrage_opportunities
            SET net_profit = max_profit * (1.0 - profit_haircut)
            WHERE max_profit != 0 AND net_profit = 0
            """
        )
        conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


def as_dict(obj: object) -> dict:
    return dict(obj.__dict__)


def load_reference_risk(
    reference_db: Path,
    max_age_seconds: int,
) -> ReferenceRisk:
    if not reference_db.exists():
        return ReferenceRisk(
            status="missing",
            label="MISSING",
            is_risky=True,
            action="missing_reference_price_data",
        )
    try:
        with connect(reference_db) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM mismatch_checks
                ORDER BY checked_ms DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
    except sqlite3.Error as exc:
        return ReferenceRisk(
            status="error",
            label="ERROR",
            is_risky=True,
            action=f"reference_price_error:{type(exc).__name__}",
        )
    if not row:
        return ReferenceRisk(
            status="missing",
            label="MISSING",
            is_risky=True,
            action="missing_reference_price_checks",
        )

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    age_seconds = max(0.0, (now_ms - int(row["checked_ms"])) / 1000)
    action = str(row["action"])
    is_stale = age_seconds > max_age_seconds
    is_risky = bool(row["is_mismatch"]) or action != "ok" or is_stale
    if is_stale:
        label = "STALE REF"
        action = "stale_reference_price_check"
    elif is_risky:
        label = action.replace("_", " ").upper()
    else:
        label = "OK"
    return ReferenceRisk(
        status="stale" if is_stale else "ok",
        label=label,
        is_risky=is_risky,
        action=action,
        checked_utc=str(row["checked_utc"]),
        age_seconds=age_seconds,
        kalshi_price=float(row["kalshi_price"]),
        polymarket_price=float(row["polymarket_price"]),
        price_diff=float(row["price_diff"]),
        abs_price_diff=float(row["abs_price_diff"]),
        time_skew_ms=int(row["time_skew_ms"]),
        mismatch_threshold=float(row["mismatch_threshold"]),
        stale_after_ms=int(row["stale_after_ms"]),
    )


def latest_poly_market(conn: sqlite3.Connection, symbol: str) -> MarketInfo | None:
    has_symbol = any(row["name"] == "symbol" for row in conn.execute("PRAGMA table_info(orderbook_snapshots)"))
    where = "WHERE symbol = ?" if has_symbol else ""
    values = (symbol,) if has_symbol else ()
    row = conn.execute(
        f"""
        SELECT m.slug, m.event_start_ts, m.end_ts
        FROM markets m
        JOIN orderbook_snapshots s ON s.slug = m.slug
        {where.replace("symbol", "s.symbol")}
        GROUP BY m.slug
        ORDER BY MAX(s.collected_ts) DESC
        LIMIT 1
        """,
        values,
    ).fetchone()
    if not row:
        return None
    return MarketInfo(
        venue="Polymarket",
        market=row["slug"],
        start_ts=int(row["event_start_ts"]),
        end_ts=int(row["end_ts"]),
    )


def latest_kalshi_market(conn: sqlite3.Connection) -> MarketInfo | None:
    row = conn.execute(
        """
        SELECT m.ticker, m.open_ts, m.close_ts
        FROM markets m
        JOIN orderbook_snapshots s ON s.ticker = m.ticker
        GROUP BY m.ticker
        ORDER BY MAX(s.collected_ts) DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return MarketInfo(
        venue="Kalshi",
        market=row["ticker"],
        start_ts=int(row["open_ts"]),
        end_ts=int(row["close_ts"]),
    )


def windows_match(first: MarketInfo, second: MarketInfo, tolerance_seconds: int) -> bool:
    return (
        abs(first.start_ts - second.start_ts) <= tolerance_seconds
        and abs(first.end_ts - second.end_ts) <= tolerance_seconds
    )


def latest_snapshot(
    conn: sqlite3.Connection,
    venue: str,
    market_column: str,
    market: MarketInfo,
    outcome: str,
) -> Snapshot | None:
    row = conn.execute(
        f"""
        SELECT *
        FROM orderbook_snapshots
        WHERE {market_column} = ? AND outcome = ?
        ORDER BY collected_ms DESC, collected_ts DESC, id DESC
        LIMIT 1
        """,
        (market.market, outcome),
    ).fetchone()
    if not row:
        return None
    snapshot_id = int(row["id"])
    levels = conn.execute(
        """
        SELECT side, price, size, level_index
        FROM orderbook_levels
        WHERE snapshot_id = ?
        ORDER BY side ASC, level_index ASC
        """,
        (snapshot_id,),
    ).fetchall()
    bids = [dict(level) for level in levels if level["side"] == "bid"]
    asks = [dict(level) for level in levels if level["side"] == "ask"]
    return Snapshot(
        venue=venue,
        market=market.market,
        outcome=outcome,
        snapshot_id=snapshot_id,
        start_ts=market.start_ts,
        end_ts=market.end_ts,
        collected_ts=int(row["collected_ts"]),
        collected_utc=str(row["collected_utc"]),
        best_bid=float(row["best_bid"]) if row["best_bid"] is not None else None,
        best_ask=float(row["best_ask"]) if row["best_ask"] is not None else None,
        bids=bids,
        asks=asks,
    )


def load_snapshots(
    poly_db: Path,
    kalshi_db: Path,
    symbol: str,
    max_market_gap_seconds: int,
    max_snapshot_age_seconds: int,
) -> tuple[dict[str, Snapshot], list[str]]:
    snapshots: dict[str, Snapshot] = {}
    warnings: list[str] = []
    now_ts = int(datetime.now(timezone.utc).timestamp())
    with connect(poly_db) as poly_conn, connect(kalshi_db) as kalshi_conn:
        poly_market = latest_poly_market(poly_conn, symbol)
        kalshi_market = latest_kalshi_market(kalshi_conn)
        if not poly_market or not kalshi_market:
            return snapshots, ["Missing latest Polymarket or Kalshi market"]
        if not windows_match(poly_market, kalshi_market, max_market_gap_seconds):
            warnings.append(
                "Market windows do not match: "
                f"Polymarket {poly_market.market} {utc_iso(poly_market.start_ts)}..{utc_iso(poly_market.end_ts)} vs "
                f"Kalshi {kalshi_market.market} {utc_iso(kalshi_market.start_ts)}..{utc_iso(kalshi_market.end_ts)}"
            )
            return snapshots, warnings
        for outcome in ("Up", "Down"):
            poly = latest_snapshot(poly_conn, "Polymarket", "slug", poly_market, outcome)
            kalshi = latest_snapshot(kalshi_conn, "Kalshi", "ticker", kalshi_market, outcome)
            if poly:
                snapshots[f"Polymarket:{outcome}"] = poly
            if kalshi:
                snapshots[f"Kalshi:{outcome}"] = kalshi
    stale = [
        f"{snapshot.venue} {snapshot.outcome} age={now_ts - snapshot.collected_ts}s"
        for snapshot in snapshots.values()
        if now_ts - snapshot.collected_ts > max_snapshot_age_seconds
    ]
    if stale:
        warnings.append("Stale snapshots blocked: " + ", ".join(stale))
        return {}, warnings
    return snapshots, warnings


def price_diff_history(
    poly_db: Path,
    kalshi_db: Path,
    symbol: str,
    limit: int = 240,
    max_time_gap_seconds: int = 2,
) -> list[dict]:
    with connect(poly_db) as poly_conn, connect(kalshi_db) as kalshi_conn:
        poly_market = latest_poly_market(poly_conn, symbol)
        kalshi_market = latest_kalshi_market(kalshi_conn)
        if not poly_market or not kalshi_market:
            return []
        if not windows_match(poly_market, kalshi_market, 60):
            return []
        poly_rows = poly_conn.execute(
            """
            WITH paired AS (
                SELECT
                    collected_ts,
                    MAX(CASE WHEN outcome = 'Up' THEN mid_price END) AS poly_up_mid,
                    MAX(CASE WHEN outcome = 'Down' THEN mid_price END) AS poly_down_mid
                FROM orderbook_snapshots
                WHERE slug = ?
                GROUP BY collected_ts
                ORDER BY collected_ts DESC
                LIMIT ?
            )
            SELECT * FROM paired
            """,
            (poly_market.market, limit),
        ).fetchall()
        kalshi_rows = kalshi_conn.execute(
            """
            WITH paired AS (
                SELECT
                    collected_ts,
                    MAX(CASE WHEN outcome = 'Up' THEN mid_price END) AS kalshi_up_mid,
                    MAX(CASE WHEN outcome = 'Down' THEN mid_price END) AS kalshi_down_mid
                FROM orderbook_snapshots
                WHERE ticker = ?
                GROUP BY collected_ts
                ORDER BY collected_ts DESC
                LIMIT ?
            )
            SELECT * FROM paired
            """,
            (kalshi_market.market, limit),
        ).fetchall()

    poly_items = [(int(row["collected_ts"]), dict(row)) for row in poly_rows]
    kalshi_items = [(int(row["collected_ts"]), dict(row)) for row in kalshi_rows]
    kalshi_items.sort(key=lambda item: item[0])
    aligned = []
    used_kalshi_ts: set[int] = set()
    for ts, poly in sorted(poly_items):
        nearest = min(kalshi_items, key=lambda item: abs(item[0] - ts), default=None)
        if not nearest:
            continue
        kalshi_ts, kalshi = nearest
        if abs(kalshi_ts - ts) > max_time_gap_seconds or kalshi_ts in used_kalshi_ts:
            continue
        used_kalshi_ts.add(kalshi_ts)
        up_diff = None
        down_diff = None
        if kalshi["kalshi_up_mid"] is not None and poly["poly_up_mid"] is not None:
            up_diff = float(kalshi["kalshi_up_mid"]) - float(poly["poly_up_mid"])
        if kalshi["kalshi_down_mid"] is not None and poly["poly_down_mid"] is not None:
            down_diff = float(kalshi["kalshi_down_mid"]) - float(poly["poly_down_mid"])
        aligned.append(
            {
                "ts": ts,
                "utc": utc_iso(ts),
                "kalshi_ts": kalshi_ts,
                "time_gap_seconds": kalshi_ts - ts,
                "up_diff": up_diff,
                "down_diff": down_diff,
                "kalshi_up_mid": kalshi["kalshi_up_mid"],
                "poly_up_mid": poly["poly_up_mid"],
                "kalshi_down_mid": kalshi["kalshi_down_mid"],
                "poly_down_mid": poly["poly_down_mid"],
            }
        )
    return aligned[-limit:]


def walk_spread(
    buy: Snapshot,
    sell: Snapshot,
    min_edge: float,
    max_slippage: float,
) -> tuple[float, float, list[dict]]:
    buy_levels = [dict(level) for level in sorted(buy.asks, key=lambda level: float(level["price"]))]
    sell_levels = [
        dict(level)
        for level in sorted(sell.bids, key=lambda level: float(level["price"]), reverse=True)
    ]
    i = j = 0
    max_size = 0.0
    max_profit = 0.0
    legs: list[dict] = []
    start_ask = float(buy_levels[0]["price"]) if buy_levels else None
    start_bid = float(sell_levels[0]["price"]) if sell_levels else None
    while i < len(buy_levels) and j < len(sell_levels):
        ask = float(buy_levels[i]["price"])
        bid = float(sell_levels[j]["price"])
        if start_ask is not None and ask - start_ask > max_slippage:
            break
        if start_bid is not None and start_bid - bid > max_slippage:
            break
        edge = bid - ask
        if edge < min_edge:
            break
        size = min(float(buy_levels[i]["size"]), float(sell_levels[j]["size"]))
        if size <= 0:
            break
        max_size += size
        max_profit += edge * size
        legs.append({"buy_price": ask, "sell_price": bid, "size": size, "edge": edge})
        buy_levels[i]["size"] = float(buy_levels[i]["size"]) - size
        sell_levels[j]["size"] = float(sell_levels[j]["size"]) - size
        if float(buy_levels[i]["size"]) <= 1e-9:
            i += 1
        if float(sell_levels[j]["size"]) <= 1e-9:
            j += 1
    return max_size, max_profit, legs


def walk_combo(
    first: Snapshot,
    second: Snapshot,
    min_edge: float,
    max_slippage: float,
) -> tuple[float, float, list[dict]]:
    first_asks = [dict(level) for level in sorted(first.asks, key=lambda level: float(level["price"]))]
    second_asks = [
        dict(level) for level in sorted(second.asks, key=lambda level: float(level["price"]))
    ]
    i = j = 0
    max_size = 0.0
    max_profit = 0.0
    legs: list[dict] = []
    start_first = float(first_asks[0]["price"]) if first_asks else None
    start_second = float(second_asks[0]["price"]) if second_asks else None
    while i < len(first_asks) and j < len(second_asks):
        p1 = float(first_asks[i]["price"])
        p2 = float(second_asks[j]["price"])
        if start_first is not None and p1 - start_first > max_slippage:
            break
        if start_second is not None and p2 - start_second > max_slippage:
            break
        edge = 1.0 - p1 - p2
        if edge < min_edge:
            break
        size = min(float(first_asks[i]["size"]), float(second_asks[j]["size"]))
        if size <= 0:
            break
        max_size += size
        max_profit += edge * size
        legs.append({"first_price": p1, "second_price": p2, "size": size, "edge": edge})
        first_asks[i]["size"] = float(first_asks[i]["size"]) - size
        second_asks[j]["size"] = float(second_asks[j]["size"]) - size
        if float(first_asks[i]["size"]) <= 1e-9:
            i += 1
        if float(second_asks[j]["size"]) <= 1e-9:
            j += 1
    return max_size, max_profit, legs


def snapshot_payload(snapshot: Snapshot) -> dict:
    return {
        "venue": snapshot.venue,
        "market": snapshot.market,
        "outcome": snapshot.outcome,
        "snapshot_id": snapshot.snapshot_id,
        "collected_utc": snapshot.collected_utc,
        "best_bid": snapshot.best_bid,
        "best_ask": snapshot.best_ask,
        "bids": snapshot.bids[:25],
        "asks": snapshot.asks[:25],
    }


def apply_profit_haircut(max_profit: float, profit_haircut: float) -> float:
    return max_profit * (1.0 - profit_haircut)


def build_opportunities(
    snapshots: dict[str, Snapshot],
    min_edge: float,
    profit_haircut: float,
    max_slippage: float,
    reference_risk: ReferenceRisk,
) -> list[dict]:
    opportunities: list[dict] = []

    for outcome in ("Up", "Down"):
        poly = snapshots.get(f"Polymarket:{outcome}")
        kalshi = snapshots.get(f"Kalshi:{outcome}")
        if not poly or not kalshi:
            continue
        for buy, sell in ((poly, kalshi), (kalshi, poly)):
            if buy.best_ask is None or sell.best_bid is None:
                continue
            edge = sell.best_bid - buy.best_ask
            if edge < min_edge:
                continue
            max_size, max_profit, legs = walk_spread(buy, sell, min_edge, max_slippage)
            if max_size <= 0:
                continue
            opportunities.append(
                {
                    "kind": "spread",
                    "outcome": outcome,
                    "buy_venue": buy.venue,
                    "sell_venue": sell.venue,
                    "buy_market": buy.market,
                    "sell_market": sell.market,
                    "edge": edge,
                    "max_size": max_size,
                    "max_profit": max_profit,
                    "profit_haircut": profit_haircut,
                    "net_profit": apply_profit_haircut(max_profit, profit_haircut),
                    "risk_label": reference_risk.label,
                    "is_risky": reference_risk.is_risky,
                    "risk_action": reference_risk.action,
                    "buy_snapshot_id": buy.snapshot_id,
                    "sell_snapshot_id": sell.snapshot_id,
                    "legs": legs,
                    "orderbooks": {"buy": snapshot_payload(buy), "sell": snapshot_payload(sell)},
                }
            )

    combo_pairs = (
        (snapshots.get("Kalshi:Up"), snapshots.get("Polymarket:Down")),
        (snapshots.get("Polymarket:Up"), snapshots.get("Kalshi:Down")),
    )
    for first, second in combo_pairs:
        if not first or not second or first.best_ask is None or second.best_ask is None:
            continue
        edge = 1.0 - first.best_ask - second.best_ask
        if edge < min_edge:
            continue
        max_size, max_profit, legs = walk_combo(first, second, min_edge, max_slippage)
        if max_size <= 0:
            continue
        opportunities.append(
            {
                "kind": "combo",
                "outcome": f"{first.outcome}+{second.outcome}",
                "buy_venue": f"{first.venue}+{second.venue}",
                "sell_venue": None,
                "buy_market": first.market,
                "sell_market": second.market,
                "edge": edge,
                "max_size": max_size,
                "max_profit": max_profit,
                "profit_haircut": profit_haircut,
                "net_profit": apply_profit_haircut(max_profit, profit_haircut),
                "risk_label": reference_risk.label,
                "is_risky": reference_risk.is_risky,
                "risk_action": reference_risk.action,
                "buy_snapshot_id": first.snapshot_id,
                "sell_snapshot_id": second.snapshot_id,
                "legs": legs,
                "orderbooks": {"first": snapshot_payload(first), "second": snapshot_payload(second)},
            }
        )

    opportunities.sort(key=lambda item: item["net_profit"], reverse=True)
    return opportunities


def record_opportunities(path: Path, opportunities: list[dict]) -> None:
    if not opportunities:
        return
    now_ts = int(datetime.now(timezone.utc).timestamp())
    now_utc = utc_iso(now_ts)
    with connect(path) as conn:
        for opp in opportunities:
            signature = (
                f"{opp['kind']}:{opp['outcome']}:{opp['buy_venue']}:{opp['sell_venue']}:"
                f"{opp['buy_snapshot_id']}:{opp['sell_snapshot_id']}:{round(opp['edge'], 6)}"
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO arbitrage_opportunities (
                    detected_ts, detected_utc, kind, outcome, buy_venue, sell_venue,
                    buy_market, sell_market, edge, max_size, max_profit, profit_haircut, net_profit,
                    risk_label, is_risky, risk_action,
                    buy_snapshot_id, sell_snapshot_id, legs_json, orderbooks_json, signature
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ts,
                    now_utc,
                    opp["kind"],
                    opp["outcome"],
                    opp["buy_venue"],
                    opp["sell_venue"],
                    opp["buy_market"],
                    opp["sell_market"],
                    opp["edge"],
                    opp["max_size"],
                    opp["max_profit"],
                    opp["profit_haircut"],
                    opp["net_profit"],
                    opp["risk_label"],
                    int(opp["is_risky"]),
                    opp["risk_action"],
                    opp["buy_snapshot_id"],
                    opp["sell_snapshot_id"],
                    json.dumps(opp["legs"], separators=(",", ":")),
                    json.dumps(opp["orderbooks"], separators=(",", ":")),
                    signature,
                ),
            )
        conn.commit()


def record_stats(path: Path) -> tuple[float, float, int, list[dict]]:
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(max_profit), 0) AS cumulative_profit,
                COALESCE(SUM(net_profit), 0) AS cumulative_net_profit,
                COUNT(*) AS count
            FROM arbitrage_opportunities
            """
        ).fetchone()
        rows = conn.execute(
            """
            SELECT detected_utc, kind, outcome, buy_venue, sell_venue, edge, max_size,
                   max_profit, profit_haircut, net_profit, risk_label, is_risky, risk_action
            FROM arbitrage_opportunities
            ORDER BY detected_ts DESC, id DESC
            LIMIT 50
            """
        ).fetchall()
    return (
        float(row["cumulative_profit"]),
        float(row["cumulative_net_profit"]),
        int(row["count"]),
        [dict(item) for item in rows],
    )


def recorded_price_diff_history(path: Path, limit: int = 240) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT detected_ts, detected_utc, outcome, orderbooks_json
            FROM arbitrage_opportunities
            WHERE kind = 'spread'
            ORDER BY detected_ts DESC, id DESC
            LIMIT ?
            """,
            (limit * 2,),
        ).fetchall()

    by_ts: dict[int, dict] = {}
    for row in reversed(rows):
        try:
            books = json.loads(row["orderbooks_json"])
        except json.JSONDecodeError:
            continue
        snapshots = [books.get("buy") or {}, books.get("sell") or {}]
        kalshi = next((item for item in snapshots if item.get("venue") == "Kalshi"), None)
        poly = next((item for item in snapshots if item.get("venue") == "Polymarket"), None)
        if not kalshi or not poly:
            continue
        if kalshi.get("best_bid") is None or kalshi.get("best_ask") is None:
            continue
        if poly.get("best_bid") is None or poly.get("best_ask") is None:
            continue
        kalshi_mid = (float(kalshi["best_bid"]) + float(kalshi["best_ask"])) / 2
        poly_mid = (float(poly["best_bid"]) + float(poly["best_ask"])) / 2
        bucket = by_ts.setdefault(
            int(row["detected_ts"]),
            {
                "ts": int(row["detected_ts"]),
                "utc": row["detected_utc"],
                "up_diff": None,
                "down_diff": None,
            },
        )
        key = "up_diff" if row["outcome"] == "Up" else "down_diff"
        bucket[key] = kalshi_mid - poly_mid
    return sorted(by_ts.values(), key=lambda item: item["ts"])[-limit:]


def build_state(
    poly_db: Path,
    kalshi_db: Path,
    reference_db: Path,
    arb_db: Path,
    symbol: str,
    min_edge: float,
    profit_haircut: float,
    max_market_gap_seconds: int = 60,
    max_snapshot_age_seconds: int = 10,
    max_slippage: float = 0.02,
    max_reference_age_seconds: int = 10,
) -> dict:
    if not poly_db.exists():
        return {"error": f"Polymarket DB does not exist: {poly_db}"}
    if not kalshi_db.exists():
        return {"error": f"Kalshi DB does not exist: {kalshi_db}"}
    init_arb_db(arb_db)
    reference_risk = load_reference_risk(reference_db, max_reference_age_seconds)
    snapshots, warnings = load_snapshots(
        poly_db,
        kalshi_db,
        symbol,
        max_market_gap_seconds,
        max_snapshot_age_seconds,
    )
    diff_history = price_diff_history(poly_db, kalshi_db, symbol)
    if reference_risk.is_risky:
        warnings.append(
            "Reference price risk: "
            f"{reference_risk.label} "
            f"BRTI={reference_risk.kalshi_price} RTDS={reference_risk.polymarket_price} "
            f"diff={reference_risk.price_diff} age={reference_risk.age_seconds}s"
        )
    opportunities = build_opportunities(
        snapshots,
        min_edge,
        profit_haircut,
        max_slippage,
        reference_risk,
    )
    record_opportunities(arb_db, opportunities)
    cumulative_profit, cumulative_net_profit, record_count, recent_records = record_stats(arb_db)
    quotes = [
        {
            "venue": snapshot.venue,
            "market": snapshot.market,
            "outcome": snapshot.outcome,
            "best_bid": snapshot.best_bid,
            "best_ask": snapshot.best_ask,
            "collected_utc": snapshot.collected_utc,
        }
        for snapshot in snapshots.values()
    ]
    latest_ts = max((snapshot.collected_ts for snapshot in snapshots.values()), default=None)
    return {
        "latest_time_utc": utc_iso(latest_ts) if latest_ts else None,
        "quotes": sorted(quotes, key=lambda item: (item["venue"], item["outcome"])),
        "price_diff_history": diff_history,
        "opportunities": opportunities,
        "best_opportunity": opportunities[0] if opportunities else None,
        "cumulative_profit": cumulative_profit,
        "cumulative_net_profit": cumulative_net_profit,
        "profit_haircut": profit_haircut,
        "max_slippage": max_slippage,
        "reference_risk": as_dict(reference_risk),
        "record_count": record_count,
        "recent_records": recent_records,
        "warnings": warnings,
    }


class Handler(BaseHTTPRequestHandler):
    poly_db: Path
    kalshi_db: Path
    reference_db: Path
    arb_db: Path
    symbol: str
    min_edge: float
    profit_haircut: float
    max_market_gap_seconds: int
    max_snapshot_age_seconds: int
    max_slippage: float
    max_reference_age_seconds: int

    def log_message(self, format: str, *args) -> None:
        return

    def send_bytes(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            payload = build_state(
                self.poly_db,
                self.kalshi_db,
                self.reference_db,
                self.arb_db,
                self.symbol,
                self.min_edge,
                self.profit_haircut,
                self.max_market_gap_seconds,
                self.max_snapshot_age_seconds,
                self.max_slippage,
                self.max_reference_age_seconds,
            )
            status = HTTPStatus.INTERNAL_SERVER_ERROR if "error" in payload else HTTPStatus.OK
            self.send_bytes(json.dumps(payload, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8", status)
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--polymarket-db", type=Path, default=DEFAULT_POLY_DB)
    parser.add_argument("--kalshi-db", type=Path, default=DEFAULT_KALSHI_DB)
    parser.add_argument("--reference-db", type=Path, default=DEFAULT_REFERENCE_DB)
    parser.add_argument("--arb-db", type=Path, default=DEFAULT_ARB_DB)
    parser.add_argument("--symbol", default="btc")
    parser.add_argument("--min-edge", type=float, default=0.001)
    parser.add_argument(
        "--profit-haircut",
        type=float,
        default=0.05,
        help="Fraction cut from gross profit for fees, mismatch, and execution risk.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument(
        "--max-market-gap-seconds",
        type=int,
        default=60,
        help="Reject arbitrage if Kalshi and Polymarket 15m market windows differ by more than this.",
    )
    parser.add_argument(
        "--max-snapshot-age-seconds",
        type=int,
        default=10,
        help="Reject arbitrage if any required quote snapshot is older than this.",
    )
    parser.add_argument(
        "--max-reference-age-seconds",
        type=int,
        default=10,
        help="Mark opportunities risky if the latest BRTI vs RTDS reference check is older than this.",
    )
    parser.add_argument(
        "--max-slippage",
        type=float,
        default=0.02,
        help="Only count order book depth within this price distance from top-of-book.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    init_arb_db(args.arb_db)
    Handler.poly_db = args.polymarket_db
    Handler.kalshi_db = args.kalshi_db
    Handler.reference_db = args.reference_db
    Handler.arb_db = args.arb_db
    Handler.symbol = args.symbol.lower()
    Handler.min_edge = args.min_edge
    Handler.profit_haircut = args.profit_haircut
    Handler.max_market_gap_seconds = args.max_market_gap_seconds
    Handler.max_snapshot_age_seconds = args.max_snapshot_age_seconds
    Handler.max_slippage = args.max_slippage
    Handler.max_reference_age_seconds = args.max_reference_age_seconds
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Arbitrage dashboard listening on http://{args.host}:{args.port}")
    print(f"Polymarket DB: {args.polymarket_db}")
    print(f"Kalshi DB: {args.kalshi_db}")
    print(f"Reference DB: {args.reference_db}")
    print(f"Arbitrage DB: {args.arb_db}")
    server.serve_forever()


if __name__ == "__main__":
    main()
