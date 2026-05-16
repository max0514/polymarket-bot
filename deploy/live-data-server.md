# Crypto 15m Order Book Data Server

Run locally:

```bash
python3 scripts/live_btc_orderbook_data_server.py \
  --host 127.0.0.1 \
  --port 8765 \
  --symbols btc,eth,sol,doge,xrp \
  --interval-seconds 0.25
```

Run the separate dashboard:

```bash
python3 scripts/web_orderbook_dashboard.py --host 127.0.0.1 --port 8767
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
GET /api/levels?snapshot_id=123
```

SQLite output:

```text
data/live_orderbooks/btc_updown_orderbooks.sqlite
```

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
