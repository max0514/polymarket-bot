"""Matching Kalshi MLB game markets to Polymarket events.

Deterministic: two teams plus a game date either identify the same game on
both venues or they don't. No model in this step - the model's job is term
extraction, later.

The Kalshi side of the fixtures is a live capture (82 real open markets); the
gamma side is hand-built to the documented shape, including its quirks:
`outcomes` and `clobTokenIds` arrive as JSON-encoded *strings*, and real feeds
contain other leagues, later games between the same teams, and occasionally
malformed rows. The matcher's contract is to pair what identifies cleanly and
skip - loudly, but without crashing - everything that does not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arb.shell.mlb_matcher import GamePairing, match_games

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def kalshi_markets() -> list[dict[str, object]]:
    markets: list[dict[str, object]] = json.loads(
        (FIXTURES / "kalshi_mlb_markets.json").read_text()
    )["markets"]
    return markets


@pytest.fixture(scope="module")
def gamma_events() -> list[dict[str, object]]:
    events: list[dict[str, object]] = json.loads(
        (FIXTURES / "gamma_mlb_events.json").read_text()
    )
    return events


@pytest.fixture(scope="module")
def pairings(
    kalshi_markets: list[dict[str, object]], gamma_events: list[dict[str, object]]
) -> list[GamePairing]:
    return list(match_games(kalshi_markets, gamma_events))


def by_kalshi_ticker(pairings: list[GamePairing], ticker: str) -> GamePairing:
    return next(p for p in pairings if p.kalshi_ticker == ticker)


class TestMatching:
    def test_matches_the_games_present_on_both_venues(
        self, pairings: list[GamePairing]
    ) -> None:
        """Two real games are in both fixtures (LAD@AZ and DET@SF on Aug 8),
        and each yields one pairing per Kalshi team market."""
        tickers = {p.kalshi_ticker for p in pairings}

        assert tickers == {
            "KXMLBGAME-26AUG082010LADAZ-LAD",
            "KXMLBGAME-26AUG082010LADAZ-AZ",
            "KXMLBGAME-26AUG081915DETSF-DET",
            "KXMLBGAME-26AUG081915DETSF-SF",
        }

    def test_the_polymarket_leg_is_the_opposite_team(
        self, pairings: list[GamePairing]
    ) -> None:
        """The arb buys complementary outcomes: Kalshi 'Dodgers win' pairs with
        the Polymarket *Diamondbacks* token, so exactly one leg pays $1.
        Pairing same-outcome tokens would be a parlay, not a hedge."""
        lad = by_kalshi_ticker(pairings, "KXMLBGAME-26AUG082010LADAZ-LAD")
        az = by_kalshi_ticker(pairings, "KXMLBGAME-26AUG082010LADAZ-AZ")

        # Fixture outcome order: ["Dodgers", "Diamondbacks"] -> [111..., 222...]
        assert lad.polymarket_token_id == "22222222222222222222"
        assert lad.polymarket_outcome == "Diamondbacks"
        assert az.polymarket_token_id == "11111111111111111111"
        assert az.polymarket_outcome == "Dodgers"

    def test_the_same_teams_on_a_different_date_do_not_match(
        self, pairings: list[GamePairing]
    ) -> None:
        """LAD@AZ also plays Aug 20 in the fixture; those tokens must not leak
        into the Aug 8 pairings."""
        for pairing in pairings:
            assert pairing.polymarket_condition_id != "0xwrong_date_condition"

    def test_non_mlb_events_are_skipped(self, pairings: list[GamePairing]) -> None:
        assert all(p.polymarket_condition_id != "0xunknown_league" for p in pairings)

    def test_malformed_token_lists_are_skipped_not_crashed(
        self, pairings: list[GamePairing]
    ) -> None:
        """One fixture row has one token for two outcomes. Skip it; a crash
        here would take the whole propose cycle down with it."""
        assert all(p.polymarket_condition_id != "0xmalformed" for p in pairings)

    def test_kalshi_games_with_no_polymarket_listing_are_simply_absent(
        self, pairings: list[GamePairing], kalshi_markets: list[dict[str, object]]
    ) -> None:
        """The live capture has 41 games; the gamma fixture lists 2 of them.
        The other 39 produce no pairing and no error."""
        assert len(pairings) == 4
        assert len(kalshi_markets) == 82


class TestWhatThePairingCarries:
    def test_it_carries_everything_the_proposer_needs(
        self, pairings: list[GamePairing]
    ) -> None:
        lad = by_kalshi_ticker(pairings, "KXMLBGAME-26AUG082010LADAZ-LAD")

        assert lad.game_date == "2026-08-08"
        assert lad.kalshi_team == "LAD"
        assert lad.kalshi_title == "Los Angeles D vs Arizona Winner?"
        assert "Dodgers" in lad.polymarket_question
        assert lad.polymarket_condition_id == "0xlad_az_condition"
        assert "resolve to the winner" in lad.polymarket_description
        assert lad.pair_id == "kxmlbgame-26aug082010ladaz-lad"

    def test_event_urls_point_at_both_venues_real_pages(
        self, pairings: list[GamePairing]
    ) -> None:
        """Kalshi's URL shape was discovered and verified in a live browser;
        Polymarket's event URL comes from the gamma slug."""
        lad = by_kalshi_ticker(pairings, "KXMLBGAME-26AUG082010LADAZ-LAD")

        assert lad.kalshi_event_url == (
            "https://kalshi.com/markets/kxmlbgame/professional-baseball-game/"
            "kxmlbgame-26aug082010ladaz"
        )
        assert lad.polymarket_event_url == (
            "https://polymarket.com/event/mlb-lad-az-2026-08-08"
        )

    def test_pairings_are_deterministically_ordered(
        self, kalshi_markets: list[dict[str, object]], gamma_events: list[dict[str, object]]
    ) -> None:
        first = [p.pair_id for p in match_games(kalshi_markets, gamma_events)]
        second = [
            p.pair_id
            for p in match_games(
                list(reversed(kalshi_markets)), list(reversed(gamma_events))
            )
        ]

        assert first == second == sorted(first)
