"""Kalshi: public REST listings and the authenticated websocket.

REST needs no auth for market data and is live-testable. The websocket
requires request signing: RSA-PSS(SHA256) over `timestamp + method + path`,
sent as three headers. `kalshi_auth_headers` is that contract, kept pure so a
test can verify the signature against the public key without any network.

Book upkeep is pure too - `(state, frame) -> (state, payload | None)` - and
the payload is exactly the shape `arb.shell.normalise.kalshi_snapshot`
already accepts, so a websocket frame flows into the same `BookSnapshot`
pipeline the rest of the system was built and tested on.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.request
from typing import Any, TypeAlias

__all__ = [
    "KALSHI_API_BASE",
    "KALSHI_WS_URL",
    "apply_kalshi_message",
    "fetch_mlb_markets",
    "kalshi_auth_headers",
]

logger = logging.getLogger(__name__)

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
_WS_SIGN_PATH = "/trade-api/ws/v2"

#: ticker -> side -> price(cents) -> quantity. Both sides are *bids*, as on
#: Kalshi's wire; the normaliser derives YES asks from NO bids.
BookState: TypeAlias = dict[str, dict[str, dict[int, int]]]

#: The normaliser-ready payload for one ticker.
BookPayload: TypeAlias = tuple[str, dict[str, Any]]


def fetch_mlb_markets(*, timeout_s: float = 15.0) -> list[dict[str, Any]]:
    """All open KXMLBGAME markets, rules text included, paging as needed."""
    markets: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(10):  # paging fuse; the league has ~15 games a day
        url = (
            f"{KALSHI_API_BASE}/markets?limit=200&status=open"
            f"&series_ticker=KXMLBGAME"
        )
        if cursor:
            url += f"&cursor={cursor}"
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            page = json.loads(response.read().decode("utf-8"))
        markets.extend(page.get("markets") or [])
        cursor = page.get("cursor") or ""
        if not cursor or not page.get("markets"):
            break
    return markets


def kalshi_auth_headers(
    *,
    key_id: str,
    private_key_pem: bytes,
    method: str,
    path: str,
    timestamp_ms: int,
) -> dict[str, str]:
    """The three headers Kalshi's authenticated endpoints check.

    Import of `cryptography` is local so that everything else in this module
    (REST, book upkeep) works without the dependency installed.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Kalshi API keys are RSA; got a different key type")

    message = f"{timestamp_ms}{method}{path}".encode("utf-8")
    signature = key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256.digest_size,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
    }


def kalshi_ws_headers(
    *, key_id: str, private_key_pem: bytes, timestamp_ms: int
) -> dict[str, str]:
    """Headers for the websocket handshake specifically."""
    return kalshi_auth_headers(
        key_id=key_id,
        private_key_pem=private_key_pem,
        method="GET",
        path=_WS_SIGN_PATH,
        timestamp_ms=timestamp_ms,
    )


def subscribe_command(tickers: list[str], command_id: int) -> str:
    """The orderbook_delta subscription frame."""
    return json.dumps(
        {
            "id": command_id,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": tickers},
        }
    )


def apply_kalshi_message(
    state: BookState, frame: dict[str, Any]
) -> tuple[BookState, BookPayload | None]:
    """Fold one websocket frame into the books.

    Returns the updated state and, when a book changed, a payload ready for
    `kalshi_snapshot`. Unknown frame types return the state untouched; a delta
    for a ticker we never got a snapshot for is dropped, because applying it
    would fabricate a book.
    """
    kind = frame.get("type")
    msg = frame.get("msg") or {}
    ticker = str(msg.get("market_ticker") or "")

    if kind == "orderbook_snapshot" and ticker:
        book = {
            "yes": _levels_to_map(msg.get("yes") or []),
            "no": _levels_to_map(msg.get("no") or []),
        }
        state = {**state, ticker: book}
        return state, (ticker, _payload(book))

    if kind == "orderbook_delta" and ticker:
        existing = state.get(ticker)
        if existing is None:
            logger.info("kalshi delta for %s before any snapshot; dropped", ticker)
            return state, None
        side = str(msg.get("side") or "")
        if side not in ("yes", "no"):
            return state, None
        try:
            price = int(msg["price"])
            delta = int(msg["delta"])
        except (KeyError, TypeError, ValueError):
            return state, None

        levels = dict(existing[side])
        quantity = levels.get(price, 0) + delta
        if quantity > 0:
            levels[price] = quantity
        else:
            levels.pop(price, None)
        book = {**existing, side: levels}
        state = {**state, ticker: book}
        return state, (ticker, _payload(book))

    return state, None


def _levels_to_map(levels: list[Any]) -> dict[int, int]:
    out: dict[int, int] = {}
    for level in levels:
        try:
            price, quantity = int(level[0]), int(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if quantity > 0:
            out[price] = quantity
    return out


def _payload(book: dict[str, dict[int, int]]) -> dict[str, Any]:
    return {
        "orderbook": {
            side: [[price, quantity] for price, quantity in sorted(levels.items())]
            for side, levels in book.items()
        }
    }
