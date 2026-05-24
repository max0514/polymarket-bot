# Polymarket Crypto Order Book Bot

This repository is a Python data and trading-research stack for Polymarket
crypto 15-minute Up/Down markets. It collects live order books, stores them in
SQLite, exposes a small HTTP API, compares Polymarket BTC markets against
Kalshi BTC 15-minute markets, and provides dashboards for monitoring live
pricing, arbitrage, and reference-price divergence.

The current codebase focuses on market data, research, and trading decision
support. It does not place live Polymarket orders by default. Treat every signal
as research until you add and test an execution layer with explicit risk limits.

Reference lineage: this repo is related to
[Cyh1368/polymarket-bot](https://github.com/Cyh1368/polymarket-bot), a public
Python fork described on GitHub as an AI prediction trading bot for Polymarket
using Claude for probability estimation. This checkout has been reshaped around
live crypto order book collection, cross-venue BTC monitoring, and reusable
local data artifacts.

## What It Does

- Discovers current Polymarket crypto 15-minute Up/Down markets.
- Polls Polymarket CLOB order books for `btc`, `eth`, `sol`, `doge`, and `xrp`.
- Stores snapshots and order book levels in SQLite.
- Serves live data through JSON endpoints for dashboards, bots, and notebooks.
- Collects Kalshi BTC 15-minute order books for cross-venue comparison.
- Tracks BTC reference-price mismatch between Kalshi BRTI and Polymarket RTDS.
- Estimates empirical BTC 15-minute divergence risk from collected data.
- Provides dashboards for live order books and Kalshi vs Polymarket arbitrage.

## System Architecture

```text
External sources
  Polymarket Gamma API
  Polymarket CLOB API
  Polymarket RTDS Chainlink websocket
  CF Benchmarks BRTI
  Kalshi BTC 15m data
  Binance BTCUSDT 15m candles

Collectors and pipelines
  scripts/live_btc_orderbook_collector.py
  scripts/live_btc_orderbook_data_server.py
  scripts/live_kalshi_btc15_orderbook_collector.py
  scripts/live_btc_reference_price_pipeline.py
  scripts/collect_btc_updown_data.py
  scripts/refetch_btc_price_history_highres.py
  scripts/estimate_btc_divergence_risk.py

Storage
  data/live_orderbooks/*.sqlite
  data/btc_updown_15m/*.csv
  data/btc_updown_15m/*.jsonl
  kalshi/kalshi_btc15m_data/*.csv

Interfaces
  JSON API on port 8765
  live order book dashboard
  Kalshi vs Polymarket arbitrage dashboard
  local notebooks and downstream scripts

Trading decision layer
  read live odds
  compare venue prices and reference-price risk
  size paper trades
  log decisions
  add explicit execution only after separate review
```

## Repository Layout

```text
scripts/
  live_btc_orderbook_data_server.py       # collector plus HTTP API
  live_btc_orderbook_collector.py         # Polymarket order book collector
  live_kalshi_btc15_orderbook_collector.py # Kalshi BTC 15m collector
  live_btc_reference_price_pipeline.py    # BRTI vs Polymarket RTDS checks
  arbitrage_dashboard.py                  # Kalshi vs Polymarket dashboard
  web_orderbook_dashboard.py              # live order book dashboard
  collect_btc_updown_data.py              # historical Polymarket/Binance data
  refetch_btc_price_history_highres.py    # high-resolution price history
  flatten_btc_price_history.py            # JSONL to CSV transform
  plot_kxbtc_vs_polymarket.py             # historical comparison plots
  estimate_btc_divergence_risk.py         # empirical risk estimates

deploy/
  live-data-server.md
  polymarket-btc-orderbook.service.example

data/
  live_orderbooks/                        # runtime SQLite files
  btc_updown_15m/                         # historical research artifacts

kalshi/
  kalshi_btc15m_data/                     # copied Kalshi BTC 15m dataset
```

## Setup

Use Python 3.10 or newer.

```bash
cd /Users/max/Desktop/python_codes/polymarket-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Run The Live Data Server

Start the combined collector and JSON API:

```bash
python3 scripts/live_btc_orderbook_data_server.py \
  --host 127.0.0.1 \
  --port 8765 \
  --symbols btc,eth,sol,doge,xrp \
  --interval-seconds 1
```

For faster local collection, lower the interval after confirming the machine and
network can keep up:

```bash
python3 scripts/live_btc_orderbook_data_server.py \
  --host 127.0.0.1 \
  --port 8765 \
  --symbols btc,eth,sol,doge,xrp \
  --interval-seconds 0.25
```

Useful endpoints:

```text
GET /health
GET /api/state
GET /api/latest
GET /api/snapshots?limit=200
GET /api/snapshots?symbol=eth&limit=200
GET /api/snapshots?outcome=Up&limit=200
GET /api/levels?symbol=eth&snapshot_id=123
```

Example:

```bash
curl http://127.0.0.1:8765/health
curl "http://127.0.0.1:8765/api/snapshots?symbol=btc&limit=20"
```

SQLite output is written under `data/live_orderbooks/`:

```text
btc_updown_orderbooks.sqlite
eth_updown_orderbooks.sqlite
sol_updown_orderbooks.sqlite
doge_updown_orderbooks.sqlite
xrp_updown_orderbooks.sqlite
```

Use `--db-dir /path/to/live_orderbooks` to change the output directory.

## Run Dashboards

Live order book dashboard:

```bash
python3 scripts/web_orderbook_dashboard.py --host 127.0.0.1 --port 8767
```

Kalshi vs Polymarket arbitrage dashboard:

```bash
python3 scripts/arbitrage_dashboard.py --host 127.0.0.1 --port 8770
```

The arbitrage dashboard applies a default profit haircut for trading fees,
execution risk, and price-window mismatch risk. Override it if needed:

```bash
python3 scripts/arbitrage_dashboard.py --profit-haircut 0.05
```

Detected opportunities are stored in:

```text
data/live_orderbooks/kalshi_polymarket_arbitrage.sqlite
```

## Kalshi And Reference-Price Monitoring

Collect Kalshi BTC 15-minute order books:

```bash
python3 scripts/live_kalshi_btc15_orderbook_collector.py \
  --interval-seconds 0.25
```

Track the reference prices that matter for BTC 15-minute settlement comparison:

```bash
python3 scripts/live_btc_reference_price_pipeline.py
```

Run a single reference-price check:

```bash
python3 scripts/live_btc_reference_price_pipeline.py --once
```

The mismatch pipeline stores raw observations and actions in:

```text
data/live_orderbooks/btc_reference_prices.sqlite
```

The current action ladder is:

```text
ok
disable_new_entries
reduce_position_and_disable_new_entries
exit_or_hedge_immediately
pause_trading_stale_source
```

## Historical Data And Research

Collect historical Polymarket BTC 15-minute labels and Binance validation
candles:

```bash
python3 scripts/collect_btc_updown_data.py
```

Refetch high-resolution Polymarket price history:

```bash
python3 scripts/refetch_btc_price_history_highres.py
```

Flatten price history JSONL into CSV:

```bash
python3 scripts/flatten_btc_price_history.py
```

Estimate empirical BTC divergence risk from collected data:

```bash
python3 scripts/estimate_btc_divergence_risk.py
```

Research artifacts are written mainly to:

```text
data/btc_updown_15m/
data/live_orderbooks/btc_divergence_risk.sqlite
data/live_orderbooks/btc_divergence_risk_latest.json
```

## Trading Workflow

This repository should be used as a trading decision-support system first, not
as an unattended live trading bot.

Recommended flow:

1. Run the live Polymarket order book server.
2. Run the Kalshi collector if comparing BTC venues.
3. Run the reference-price mismatch pipeline.
4. Monitor `/api/latest`, `/api/state`, and the dashboards.
5. Generate a paper-trading decision from:
   - best bid and best ask on each outcome,
   - spread and available size,
   - cross-venue edge after haircut,
   - reference-price mismatch action,
   - time remaining in the 15-minute window,
   - bankroll and max-loss limits.
6. Log every candidate trade and rejected trade.
7. Only add live execution after paper results, fill assumptions, and loss
   controls are verified.

Minimum controls for any future execution layer:

- Default to paper trading.
- Require an explicit `--live` flag for real orders.
- Cap max position per market and per venue.
- Skip markets with stale data or wide spreads.
- Disable new entries when the reference pipeline returns a risk action.
- Exit or hedge immediately on severe reference-price mismatch.
- Store every signal, order attempt, fill, and cancellation in SQLite.

The live execution layer is intentionally not implemented in the current tree
after removing the old legacy code. A clean trading module should consume the
HTTP API and SQLite outputs rather than mixing order execution into collectors.

## Run On A Server

Basic foreground run:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 scripts/live_btc_orderbook_data_server.py \
  --host 0.0.0.0 \
  --port 8765 \
  --symbols btc,eth,sol,doge,xrp \
  --interval-seconds 1
```

For systemd deployments, start from:

```bash
sudo cp deploy/polymarket-btc-orderbook.service.example /etc/systemd/system/polymarket-btc-orderbook.service
sudo systemctl daemon-reload
sudo systemctl enable polymarket-btc-orderbook
sudo systemctl start polymarket-btc-orderbook
sudo systemctl status polymarket-btc-orderbook
```

Edit the service file first if the repo is not deployed at:

```text
/opt/polymarket-bot
```

See [deploy/live-data-server.md](deploy/live-data-server.md) for the shorter
server command reference.

## Safety Notes

- This is not financial advice.
- Prediction-market execution has fill risk, latency risk, settlement risk, and
  venue-specific rule risk.
- BTC 15-minute markets are especially sensitive to seconds-level source,
  timestamp, and rounding differences.
- Keep real money disabled until the paper-trading log proves the strategy
  survives fees, slippage, stale data, and failed fills.
