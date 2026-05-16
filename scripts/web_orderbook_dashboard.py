"""Standalone web dashboard for the BTC order book data server.

This process does not collect data and does not read SQLite directly. It serves
the UI and proxies /api/* requests to live_btc_orderbook_data_server.py.

Run data server:
  python3 scripts/live_btc_orderbook_data_server.py --host 127.0.0.1 --port 8765

Run dashboard:
  python3 scripts/web_orderbook_dashboard.py --host 127.0.0.1 --port 8767
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from live_orderbook_dashboard import HTML  # noqa: E402


class DashboardHandler(BaseHTTPRequestHandler):
    data_server_url: str

    def log_message(self, format: str, *args) -> None:
        return

    def send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self) -> None:
        self.send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")

    def proxy_api(self) -> None:
        target = urljoin(self.data_server_url, self.path)
        try:
            with urllib.request.urlopen(target, timeout=5) as response:
                body = response.read()
                content_type = response.headers.get(
                    "Content-Type", "application/json; charset=utf-8"
                )
                self.send_bytes(body, content_type, HTTPStatus(response.status))
        except urllib.error.HTTPError as error:
            self.send_bytes(
                error.read(),
                error.headers.get("Content-Type", "application/json; charset=utf-8"),
                HTTPStatus(error.code),
            )
        except Exception as error:
            body = (
                f'{{"error":"data server unavailable: '
                f'{type(error).__name__}: {str(error)}"}}'
            ).encode("utf-8")
            self.send_bytes(body, "application/json; charset=utf-8", HTTPStatus.BAD_GATEWAY)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_html()
            return
        if path.startswith("/api/") or path == "/health":
            self.proxy_api()
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--data-server-url", default="http://127.0.0.1:8765")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    DashboardHandler.data_server_url = args.data_server_url.rstrip("/") + "/"
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard listening on http://{args.host}:{args.port}")
    print(f"Proxying data server: {DashboardHandler.data_server_url}")
    server.serve_forever()


if __name__ == "__main__":
    main()
