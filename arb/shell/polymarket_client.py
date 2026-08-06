"""Polymarket: gamma REST listings and the public CLOB market websocket.

This sandbox cannot reach Polymarket at all, so unlike the Kalshi client
nothing here has run against the live venue - it is written to the documented
API shapes, tested on fixtures, and deliberately defensive: any frame or row
that does not parse is logged and skipped, never raised. The first live run
happens on the operator's machine, and a wrong assumption should cost one log
line there, not the collection run.

Book upkeep mirrors the Kalshi client: pure `(state, frame) -> (state,
payload | None)`, payload in exactly the shape `polymarket_snapshot` accepts.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, TypeAlias

__all__ = [
    "GAMMA_API_BASE",
    "POLYMARKET_WS_URL",
    "apply_polymarket_message",
    "fetch_mlb_events",
    "subscribe_command",
]

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

#: token id -> {"bids"|"asks" -> {price(str) -> size(str)}, "timestamp" -> str}
BookState: TypeAlias = dict[str, dict[str, Any]]
BookPayload: TypeAlias = tuple[str, dict[str, Any]]


def fetch_mlb_events(*, timeout_s: float = 15.0) -> list[dict[str, Any]]:
    """Open MLB events from gamma.

    `tag_slug=mlb` is the documented filter; if the tag taxonomy differs in
    practice, the matcher's own slug check (`mlb-` prefix) still keeps foreign
    events out - this filter is bandwidth, not correctness.
    """
    url = f"{GAMMA_API_BASE}/events?closed=false&limit=300&tag_slug=mlb"
    with urllib.request.urlopen(url, timeout=timeout_s) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        logger.warning("gamma events response was not a list; treating as empty")
        return []
    return [event for event in payload if isinstance(event, dict)]


def subscribe_command(token_ids: list[str]) -> str:
    """The market-channel subscription frame (public; no auth)."""
    return json.dumps({"type": "market", "assets_ids": token_ids})


def apply_polymarket_message(
    state: BookState, frame: dict[str, Any]
) -> tuple[BookState, BookPayload | None]:
    """Fold one market-channel frame into the books.

    `book` frames replace a token's book wholesale; `price_change` frames set
    single levels (size 0 removes). A change for a token we never got a book
    for is dropped rather than fabricated.
    """
    kind = frame.get("event_type")
    token = str(frame.get("asset_id") or "")

    if kind == "book" and token:
        book = {
            "bids": _levels_to_map(frame.get("bids")),
            "asks": _levels_to_map(frame.get("asks")),
            "timestamp": str(frame.get("timestamp") or ""),
        }
        state = {**state, token: book}
        return state, (token, _payload(book))

    if kind == "price_change" and token:
        existing = state.get(token)
        if existing is None:
            logger.info("polymarket change for %s before any book; dropped", token)
            return state, None
        sides: dict[str, dict[str, str]] = {
            "bids": dict(existing["bids"]),
            "asks": dict(existing["asks"]),
        }
        for change in frame.get("changes") or frame.get("price_changes") or []:
            if not isinstance(change, dict):
                continue
            side = {"BUY": "bids", "SELL": "asks"}.get(str(change.get("side")))
            price = change.get("price")
            size = change.get("size")
            if side is None or price is None or size is None:
                continue
            try:
                remove = float(size) <= 0
            except ValueError:
                continue
            if remove:
                sides[side].pop(str(price), None)
            else:
                sides[side][str(price)] = str(size)
        book = {
            "bids": sides["bids"],
            "asks": sides["asks"],
            "timestamp": str(frame.get("timestamp") or existing.get("timestamp", "")),
        }
        state = {**state, token: book}
        return state, (token, _payload(book))

    return state, None


def _levels_to_map(levels: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for level in levels or []:
        if not isinstance(level, dict):
            continue
        price = level.get("price")
        size = level.get("size")
        if price is None or size is None:
            continue
        out[str(price)] = str(size)
    return out


def _payload(book: dict[str, Any]) -> dict[str, Any]:
    return {
        "bids": [
            {"price": price, "size": size} for price, size in book["bids"].items()
        ],
        "asks": [
            {"price": price, "size": size} for price, size in book["asks"].items()
        ],
        "timestamp": book.get("timestamp") or None,
    }
