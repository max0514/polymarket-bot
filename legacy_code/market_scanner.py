"""Connect to Polymarket CLOB API and filter relevant markets."""

import logging
from datetime import datetime, timedelta

import httpx

from .models import Market

logger = logging.getLogger(__name__)

CLOB_BASE_URL = "https://clob.polymarket.com"
ALLOWED_CATEGORIES = {"tech", "technology", "business", "ai", "artificial intelligence", "crypto"}
MIN_VOLUME = 1000.0  # $1,000 USD
MIN_DAYS_TO_CLOSE = 3


async def fetch_markets(
    api_key: str | None = None,
    min_volume: float = MIN_VOLUME,
    min_days: int = MIN_DAYS_TO_CLOSE,
) -> list[Market]:
    """Fetch and filter markets from Polymarket CLOB API."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    markets = []
    next_cursor = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            params: dict = {"limit": 100}
            if next_cursor:
                params["next_cursor"] = next_cursor

            try:
                resp = await client.get(
                    f"{CLOB_BASE_URL}/markets", headers=headers, params=params
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.error("Failed to fetch markets: %s", e)
                break

            data = resp.json()
            raw_markets = data if isinstance(data, list) else data.get("data", [])

            for m in raw_markets:
                market = _parse_market(m, min_volume, min_days)
                if market:
                    markets.append(market)

            # Handle pagination
            if isinstance(data, dict) and data.get("next_cursor"):
                next_cursor = data["next_cursor"]
            else:
                break

    logger.info("Found %d qualifying markets", len(markets))
    return markets


def _parse_market(
    raw: dict, min_volume: float, min_days: int
) -> Market | None:
    """Parse and filter a single market from API response."""
    try:
        # Check for binary YES/NO market
        tokens = raw.get("tokens", [])
        if len(tokens) != 2:
            return None

        # Find YES token price
        yes_price = None
        for token in tokens:
            if token.get("outcome", "").upper() == "YES":
                yes_price = float(token.get("price", 0))
                break

        if yes_price is None:
            return None

        # Check volume
        volume = float(raw.get("volume_num_24hr", raw.get("volume24hr", 0)))
        if volume < min_volume:
            return None

        # Check category
        category = (raw.get("category", "") or "").lower()
        tags = [t.lower() for t in raw.get("tags", [])]
        category_match = category in ALLOWED_CATEGORIES or any(
            t in ALLOWED_CATEGORIES for t in tags
        )
        if not category_match:
            return None

        # Check end date
        end_date_str = raw.get("end_date_iso", raw.get("end_date", ""))
        if not end_date_str:
            return None

        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        if end_date < datetime.now(end_date.tzinfo) + timedelta(days=min_days):
            return None

        question = raw.get("question", "")
        market_id = raw.get("condition_id", raw.get("id", ""))

        return Market(
            market_id=market_id,
            question=question,
            yes_price=yes_price,
            volume_24h=volume,
            end_date=end_date,
            category=category or (tags[0] if tags else "unknown"),
        )

    except (KeyError, ValueError, TypeError) as e:
        logger.debug("Skipping market: %s", e)
        return None
