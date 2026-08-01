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
- Provides dashboards for live order books and Kalshi vs Polymarket arbitrage.
- Evaluates cross-venue arbitrage candidates against real fees in `arb/`, and
  logs every evaluation - accepted or rejected - so a base rate can be computed.

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

Storage
  data/live_orderbooks/*.sqlite
  data/btc_updown_15m/*.csv
  data/btc_updown_15m/*.jsonl

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

arb/                                      # cross-venue arbitrage decision core
  reducer.py                              # the seam: step(State, Event)
  pricing.py sizing.py evaluate.py        # fees, Net Edge, marginal-stop walk
  verification.py registry.py             # pair matching and approval
  risk.py inventory.py                    # flags, budgets, Drift steering
  legging.py execution.py exits.py        # order placement and recovery
  settlement.py report.py replay.py       # reconciliation and the verdict
  shell/                                  # clients, persistence, runtime loop

tests/                                    # pytest suite for the above

deploy/
  live-data-server.md
  polymarket-btc-orderbook.service.example

data/
  live_orderbooks/                        # runtime SQLite files
  btc_updown_15m/                         # historical research artifacts
```

Note: `scripts/estimate_btc_divergence_risk.py` and the `kalshi/` data tree are
referenced by older notes but are not present in this checkout.

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

Research artifacts are written mainly to:

```text
data/btc_updown_15m/
```

## Cross-Venue Arbitrage Decision Core

`arb/` is a separate system from the collectors above, built to answer one
question: does a capturable arbitrage exist between Kalshi and Polymarket?

It is a functional core with an imperative shell. All logic sits behind one
reducer, `step(State, Event) -> (State, Action[])`, which is pure - no clock,
no I/O, no randomness. Time and connectivity arrive as events, so a recorded
event log replays to a byte-identical action trace. That single property makes
the regression suite, the backtest, and the evidence behind the verdict the
same artifact.

Two things distinguish it from the arbitrage dashboard in `scripts/`:

- **Net Edge can be negative.** Fees are subtracted at the real per-contract
  rate on each leg, `(0.07 + theta) * p * (1 - p)`, rather than applied as a
  proportional haircut to a quantity that is already non-negative. The
  dashboard's `--profit-haircut` cannot mark anything unprofitable.
- **Rejections are persisted.** Every evaluated candidate is written with its
  fee breakdown and rejection reason, so a base rate has a denominator.

Run the tests:

```bash
python3 -m pytest
```

Typecheck:

```bash
python3 -m mypy
```

Execution defaults to a dry run: `arb.shell.runtime.DryRunGateway` records
order intent and sends nothing, so a verdict can be collected with no capital
at risk. Live order routing requires supplying a real `OrderGateway`.

Unresolved before this system can produce a verdict, both flagged in the spec:
the verdict criteria themselves (no threshold, observation window, or stopping
rule has been set) and total capital with its per-venue split, which every
balance floor and concentration budget depends on. All are configuration in
`arb.config.Config` and `arb.risk.RiskLimits`, with no defaults invented.

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
