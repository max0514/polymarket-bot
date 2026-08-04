# Crypto 15m Order Book Data Server

Run locally:

```bash
python3 scripts/live_btc_orderbook_data_server.py \
  --host 127.0.0.1 \
  --port 8765 \
  --symbols btc,eth,sol,doge,xrp \
  --interval-seconds 0.25
```

Collect Kalshi BTC 15-minute Up/Down order books with the same 0.25 second
interval:

```bash
python3 scripts/live_kalshi_btc15_orderbook_collector.py \
  --interval-seconds 0.25
```

Run the separate dashboard:

```bash
python3 scripts/web_orderbook_dashboard.py --host 127.0.0.1 --port 8767
```

Run the pair review screen:

```bash
python3 -m arb.shell.review_server --operator YOUR_NAME --port 8771
```

The dashboard applies a default 5% profit haircut for trading fees, execution
risk, and possible Kalshi/Polymarket price-window mismatch:


Detected arbitrage opportunities are recorded here:

```text
data/live_orderbooks/pair_candidates.sqlite
```

Run on a server:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/live_btc_orderbook_data_server.py \
  --host 0.0.0.0 \
  --port 8765 \
  --symbols btc,eth,sol,doge,xrp \
  --interval-seconds 0.25
```

Endpoints:

```text
GET /health
GET /api/state
GET /api/latest
GET /api/snapshots?limit=200
GET /api/snapshots?symbol=eth&limit=200
GET /api/snapshots?outcome=Up&limit=200
GET /api/levels?symbol=eth&snapshot_id=123
```

SQLite output:

```text
data/live_orderbooks/btc_updown_orderbooks.sqlite
data/live_orderbooks/eth_updown_orderbooks.sqlite
data/live_orderbooks/sol_updown_orderbooks.sqlite
data/live_orderbooks/doge_updown_orderbooks.sqlite
data/live_orderbooks/xrp_updown_orderbooks.sqlite
data/live_orderbooks/kalshi_btc15_orderbooks.sqlite
```

Use `--db-dir /path/to/live_orderbooks` to choose the directory for per-coin
SQLite files. `--db /path/to/file.sqlite` is only for one-symbol runs.

Systemd:

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
