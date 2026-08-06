"""Polymarket: gamma REST listings and the public CLOB market websocket.

The gamma listing fetch has run against the live venue (2026-08-06); the
websocket half still has not - it is written to the documented API shapes,
tested on fixtures, and deliberately defensive: any frame or row that does
not parse is logged and skipped, never raised. Its first live run happens on
the operator's machine, and a wrong assumption should cost one log line
there, not the collection run.

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


#: Gamma serves at most this many events per request regardless of `limit`,
#: so listing is a paged walk, not one call.
_GAMMA_PAGE_SIZE = 100

#: Refusing to walk forever if gamma keeps returning full pages; 50 pages is
#: 5000 open MLB events - far beyond a real season's listing.
_GAMMA_MAX_PAGES = 50


def fetch_mlb_events(*, timeout_s: float = 15.0) -> list[dict[str, Any]]:
    """Open MLB events from gamma, walking every page.

    `tag_slug=mlb` is the documented filter; if the tag taxonomy differs in
    practice, the matcher's own slug check (`mlb-` prefix) still keeps foreign
    events out - this filter is bandwidth, not correctness.
    """
    events: list[dict[str, Any]] = []
    for page in range(_GAMMA_MAX_PAGES):
        url = (
            f"{GAMMA_API_BASE}/events?closed=false&limit={_GAMMA_PAGE_SIZE}"
            f"&tag_slug=mlb&offset={page * _GAMMA_PAGE_SIZE}"
        )
        # Gamma's CDN answers 403 to urllib's default "Python-urllib" agent,
        # so the fetch must say who it is.
        request = urllib.request.Request(
            url, headers={"User-Agent": "arb-collector/1.0"}
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            logger.warning("gamma events response was not a list; treating as empty")
            break
        events.extend(event for event in payload if isinstance(event, dict))
        if len(payload) < _GAMMA_PAGE_SIZE:
            return events
    else:
        logger.warning(
            "gamma listing still returning full pages after %d pages; "
            "events beyond offset %d were not fetched",
            _GAMMA_MAX_PAGES,
            _GAMMA_MAX_PAGES * _GAMMA_PAGE_SIZE,
        )
    return events


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
