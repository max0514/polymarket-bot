"""Pairing Kalshi MLB game markets with Polymarket events.

Deterministic, table-driven. Two teams plus a game date either identify the
same game on both venues or they do not - no model is involved in matching.
(The model's job is term *extraction*, downstream.)

The one non-obvious decision is outcome polarity. A Matched Pair buys
complementary outcomes so that exactly one leg pays $1: Kalshi "Dodgers win"
pairs with the Polymarket *Diamondbacks* token. Pairing same-outcome tokens
would pay double-or-nothing on one team - a parlay, not a hedge - and the
gross-edge formula `1 - p1 - p2` would be nonsense.

Everything that does not identify cleanly - other leagues, unknown team codes,
token lists that disagree with outcome lists, dates that miss - is skipped and
counted, never raised: one odd row on a venue feed must not take the propose
cycle down.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

__all__ = ["GamePairing", "match_games"]

logger = logging.getLogger(__name__)

#: Kalshi team code -> (Polymarket slug codes, name fragments seen in titles
#: and outcome labels). Slug codes carry known variants; names are lowercase
#: substrings. Both venues' labels are matched case-insensitively.
MLB_TEAMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "ATH": (("ath", "oak"), ("athletics", "a's")),
    "ATL": (("atl",), ("braves", "atlanta")),
    "AZ": (("az", "ari"), ("diamondbacks", "arizona")),
    "BAL": (("bal",), ("orioles", "baltimore")),
    "BOS": (("bos",), ("red sox", "boston")),
    "CHC": (("chc",), ("cubs",)),
    "CIN": (("cin",), ("reds", "cincinnati")),
    "CLE": (("cle",), ("guardians", "cleveland")),
    "COL": (("col",), ("rockies", "colorado")),
    "CWS": (("cws", "chw"), ("white sox",)),
    "DET": (("det",), ("tigers", "detroit")),
    "HOU": (("hou",), ("astros", "houston")),
    "KC": (("kc", "kcr"), ("royals", "kansas city")),
    "LAA": (("laa",), ("angels",)),
    "LAD": (("lad",), ("dodgers",)),
    "MIA": (("mia",), ("marlins", "miami")),
    "MIL": (("mil",), ("brewers", "milwaukee")),
    "MIN": (("min",), ("twins", "minnesota")),
    "NYM": (("nym",), ("mets",)),
    "NYY": (("nyy",), ("yankees",)),
    "PHI": (("phi",), ("phillies", "philadelphia")),
    "PIT": (("pit",), ("pirates", "pittsburgh")),
    "SD": (("sd", "sdp"), ("padres", "san diego")),
    "SEA": (("sea",), ("mariners", "seattle")),
    "SF": (("sf", "sfg"), ("giants", "san francisco")),
    "STL": (("stl",), ("cardinals", "st. louis")),
    "TB": (("tb", "tbr"), ("rays", "tampa bay")),
    "TEX": (("tex",), ("rangers", "texas")),
    "TOR": (("tor",), ("blue jays", "toronto")),
    "WSH": (("wsh", "was"), ("nationals", "washington")),
}

_SLUG_TO_KALSHI = {
    slug: kalshi
    for kalshi, (slugs, _) in MLB_TEAMS.items()
    for slug in slugs
}

#: Game dates are compared in the venue's home time zone. Kalshi encodes the
#: start in ET inside the ticker; gamma publishes UTC. Fixed -4 (EDT) is exact
#: for the baseball season, which ends before the November clock change.
_ET = timezone(timedelta(hours=-4))

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


@dataclass(frozen=True, slots=True)
class GamePairing:
    """One Kalshi team market matched to its complementary Polymarket token."""

    pair_id: str
    game_date: str
    kalshi_ticker: str
    kalshi_event_ticker: str
    kalshi_team: str
    kalshi_title: str
    kalshi_event_url: str
    #: The token for the OTHER team - the complementary outcome.
    polymarket_token_id: str
    polymarket_outcome: str
    polymarket_condition_id: str
    polymarket_question: str
    polymarket_description: str
    polymarket_event_url: str


def match_games(
    kalshi_markets: Iterable[dict[str, Any]],
    gamma_events: Iterable[dict[str, Any]],
) -> Iterator[GamePairing]:
    """Yield pairings for every game listed on both venues, sorted by pair id."""
    games = _kalshi_games(kalshi_markets)
    skipped = 0
    pairings: list[GamePairing] = []

    for event in gamma_events:
        parsed = _parse_gamma_event(event)
        if parsed is None:
            skipped += 1
            continue
        teams, date, market, event_slug = parsed

        game = games.get((frozenset(teams), date))
        if game is None:
            continue

        outcome_tokens = _outcome_tokens(market, teams)
        if outcome_tokens is None:
            skipped += 1
            continue

        for kalshi_team, kalshi_market in game.items():
            other = next(t for t in teams if t != kalshi_team)
            token_id, outcome_label = outcome_tokens[other]
            event_ticker = kalshi_market["event_ticker"]
            pairings.append(
                GamePairing(
                    pair_id=f"{event_ticker.lower()}-{kalshi_team.lower()}",
                    game_date=date,
                    kalshi_ticker=kalshi_market["ticker"],
                    kalshi_event_ticker=event_ticker,
                    kalshi_team=kalshi_team,
                    kalshi_title=kalshi_market.get("title", ""),
                    kalshi_event_url=(
                        "https://kalshi.com/markets/kxmlbgame/"
                        f"professional-baseball-game/{event_ticker.lower()}"
                    ),
                    polymarket_token_id=token_id,
                    polymarket_outcome=outcome_label,
                    polymarket_condition_id=market.get("conditionId", ""),
                    polymarket_question=market.get("question", ""),
                    polymarket_description=market.get("description", ""),
                    polymarket_event_url=f"https://polymarket.com/event/{event_slug}",
                )
            )

    if skipped:
        logger.info("mlb matcher skipped %d gamma events it could not identify", skipped)
    return iter(sorted(pairings, key=lambda p: p.pair_id))


def _kalshi_games(
    markets: Iterable[dict[str, Any]],
) -> dict[tuple[frozenset[str], str], dict[str, dict[str, Any]]]:
    """Group Kalshi team markets by (team pair, ET game date).

    The team comes from the market ticker's suffix rather than by splitting the
    concatenated pair in the event ticker, which would be ambiguous ("LADAZ").
    """
    games: dict[tuple[frozenset[str], str], dict[str, dict[str, Any]]] = {}
    by_event: dict[str, dict[str, dict[str, Any]]] = {}
    for market in markets:
        ticker = market.get("ticker", "")
        if "-" not in ticker:
            continue
        team = ticker.rsplit("-", 1)[1]
        if team not in MLB_TEAMS:
            logger.info("unknown kalshi team code %r in %s", team, ticker)
            continue
        by_event.setdefault(market.get("event_ticker", ""), {})[team] = market

    for event_ticker, teams in by_event.items():
        if len(teams) != 2:
            continue
        date = _kalshi_event_date(event_ticker)
        if date is None:
            continue
        games[(frozenset(teams), date)] = teams
    return games


def _kalshi_event_date(event_ticker: str) -> str | None:
    """`KXMLBGAME-26AUG082010LADAZ` -> `2026-08-08` (the ET start date)."""
    try:
        stamp = event_ticker.split("-")[1]
        year = 2000 + int(stamp[0:2])
        month = _MONTHS[stamp[2:5]]
        day = int(stamp[5:7])
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (IndexError, KeyError, ValueError):
        logger.info("unparseable kalshi event ticker %r", event_ticker)
        return None


def _parse_gamma_event(
    event: dict[str, Any],
) -> tuple[tuple[str, str], str, dict[str, Any], str] | None:
    """Identify a gamma event as (two kalshi team codes, ET date, market, slug)."""
    slug = str(event.get("slug", ""))
    parts = slug.split("-")
    if len(parts) < 6 or parts[0] != "mlb":
        return None

    away = _SLUG_TO_KALSHI.get(parts[1])
    home = _SLUG_TO_KALSHI.get(parts[2])
    if away is None or home is None or away == home:
        return None

    date = _gamma_event_date(event, slug_date="-".join(parts[3:6]))
    markets = event.get("markets") or []
    if date is None or not markets:
        return None
    return (away, home), date, markets[0], slug


def _gamma_event_date(event: dict[str, Any], slug_date: str) -> str | None:
    """Prefer the start time converted to ET; fall back to the slug's date."""
    raw = event.get("startDate") or event.get("gameStartTime")
    if raw:
        try:
            started = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return started.astimezone(_ET).date().isoformat()
        except ValueError:
            pass
    try:
        datetime.fromisoformat(slug_date)
        return slug_date
    except ValueError:
        return None


def _outcome_tokens(
    market: dict[str, Any], teams: tuple[str, str]
) -> dict[str, tuple[str, str]] | None:
    """Map each Kalshi team code to (token id, outcome label) via name matching.

    `outcomes` and `clobTokenIds` arrive as JSON-encoded strings on gamma.
    """
    try:
        outcomes = json.loads(market.get("outcomes") or "[]")
        tokens = json.loads(market.get("clobTokenIds") or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if len(outcomes) != 2 or len(tokens) != 2:
        return None

    mapping: dict[str, tuple[str, str]] = {}
    for outcome, token in zip(outcomes, tokens):
        label = str(outcome).casefold()
        matched = [
            team
            for team in teams
            if any(name in label for name in MLB_TEAMS[team][1])
        ]
        if len(matched) != 1:
            return None
        mapping[matched[0]] = (str(token), str(outcome))

    if set(mapping) != set(teams):
        return None
    return mapping
