"""Venue payloads to `BookSnapshot`.

Shell code, and the only place that knows what either venue's wire format looks
like. Everything past this boundary sees one book shape.

The two venues disagree about almost everything mechanical:

* **Kalshi** quotes in whole cents and publishes only *bids*, on both the YES
  and NO sides. The ask for YES is therefore derived: someone bidding 30c for
  NO is offering YES at 70c. Failing to derive it would leave the YES book
  looking one-sided and untradeable.
* **Polymarket** quotes decimal strings on both sides directly, and sizes are
  fractional shares rather than whole contracts.

Both snapshots are stamped with a local receipt time supplied by the caller,
because that is the only clock the two venues can be compared on.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Sequence

from arb.domain import BookSnapshot, Level

__all__ = ["kalshi_snapshot", "polymarket_snapshot"]

ONE = Decimal("1")
CENTS = Decimal("100")


def kalshi_snapshot(
    payload: dict[str, Any],
    *,
    contract_id: str,
    received_time_ms: int,
    venue_time_ms: int | None = None,
) -> BookSnapshot:
    """Normalise a Kalshi orderbook for the YES side of one market.

    `payload` is the venue's `orderbook` object: `{"yes": [[cents, qty], ...],
    "no": [[cents, qty], ...]}`, both sides being bids.
    """
    book = payload.get("orderbook", payload)
    yes_bids = _kalshi_levels(book.get("yes") or [])
    no_bids = _kalshi_levels(book.get("no") or [])

    # A bid of `p` for NO is an offer of YES at `1 - p`.
    asks = tuple(
        Level(price=ONE - level.price, size=level.size) for level in no_bids
    )

    return BookSnapshot(
        venue="kalshi",
        contract_id=contract_id,
        asks=tuple(sorted(asks, key=lambda level: level.price)),
        bids=tuple(sorted(yes_bids, key=lambda level: level.price, reverse=True)),
        venue_time_ms=received_time_ms if venue_time_ms is None else venue_time_ms,
        received_time_ms=received_time_ms,
    )


def polymarket_snapshot(
    payload: dict[str, Any],
    *,
    contract_id: str,
    received_time_ms: int,
    venue_time_ms: int | None = None,
) -> BookSnapshot:
    """Normalise a Polymarket CLOB book.

    `payload` is `{"bids": [{"price": "0.30", "size": "100"}, ...], "asks":
    [...], "timestamp": "..."}`.
    """
    stamped = venue_time_ms
    if stamped is None:
        raw = payload.get("timestamp")
        stamped = int(raw) if raw is not None else received_time_ms

    return BookSnapshot(
        venue="polymarket",
        contract_id=contract_id,
        asks=tuple(
            sorted(
                _polymarket_levels(payload.get("asks") or []),
                key=lambda level: level.price,
            )
        ),
        bids=tuple(
            sorted(
                _polymarket_levels(payload.get("bids") or []),
                key=lambda level: level.price,
                reverse=True,
            )
        ),
        venue_time_ms=stamped,
        received_time_ms=received_time_ms,
    )


def _kalshi_levels(raw: Iterable[Sequence[Any]]) -> tuple[Level, ...]:
    return tuple(
        level
        for level in (
            Level(price=Decimal(str(entry[0])) / CENTS, size=int(entry[1]))
            for entry in raw
            if len(entry) >= 2
        )
        if level.size > 0
    )


def _polymarket_levels(raw: Iterable[dict[str, Any]]) -> tuple[Level, ...]:
    levels = []
    for entry in raw:
        price = entry.get("price")
        size = entry.get("size")
        if price is None or size is None:
            continue
        # Sizes are fractional shares; a partial contract cannot be paired
        # against a whole Kalshi contract, so it is floored rather than rounded.
        contracts = int(Decimal(str(size)))
        if contracts > 0:
            levels.append(Level(price=Decimal(str(price)), size=contracts))
    return tuple(levels)
