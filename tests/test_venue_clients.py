"""Venue clients: Kalshi auth signing + both venues' websocket book upkeep.

Book maintenance is pure - (state, message) -> (state, payload) - and the
payloads feed the *existing* normalisers, so these tests close the loop from a
raw websocket frame to a `BookSnapshot` the reducer accepts.

The Kalshi REST listing test runs against the live API (it is public and this
sandbox reaches it) and skips - not fails - when offline, so the suite stays
runnable anywhere.
"""

from __future__ import annotations

import base64
import urllib.error
from decimal import Decimal

import pytest

from arb.shell.kalshi_client import (
    BookState as KalshiBooks,
    apply_kalshi_message,
    fetch_mlb_markets,
    kalshi_auth_headers,
)
from arb.shell.normalise import kalshi_snapshot, polymarket_snapshot
from arb.shell.polymarket_client import (
    BookState as PolymarketBooks,
    apply_polymarket_message,
)


class TestKalshiSigning:
    def test_the_signature_verifies_against_the_public_key(self) -> None:
        """The contract Kalshi checks: RSA-PSS(SHA256) over ts+method+path."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

        headers = kalshi_auth_headers(
            key_id="my-key-id",
            private_key_pem=pem,
            method="GET",
            path="/trade-api/ws/v2",
            timestamp_ms=1_754_500_000_000,
        )

        assert headers["KALSHI-ACCESS-KEY"] == "my-key-id"
        assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1754500000000"
        key.public_key().verify(  # raises InvalidSignature if wrong
            base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
            b"1754500000000GET/trade-api/ws/v2",
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256.digest_size,
            ),
            hashes.SHA256(),
        )

    def test_two_timestamps_sign_differently(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

        first = kalshi_auth_headers(
            key_id="k", private_key_pem=pem, method="GET",
            path="/trade-api/ws/v2", timestamp_ms=1,
        )
        second = kalshi_auth_headers(
            key_id="k", private_key_pem=pem, method="GET",
            path="/trade-api/ws/v2", timestamp_ms=2,
        )

        assert first["KALSHI-ACCESS-SIGNATURE"] != second["KALSHI-ACCESS-SIGNATURE"]


class TestKalshiBookUpkeep:
    def test_a_snapshot_then_deltas_produce_the_net_book(self) -> None:
        state: KalshiBooks = {}
        state, _ = apply_kalshi_message(
            state,
            {
                "type": "orderbook_snapshot",
                "msg": {
                    "market_ticker": "KXMLBGAME-26AUG082010LADAZ-LAD",
                    "yes": [[40, 100], [39, 50]],
                    "no": [[58, 200]],
                },
            },
        )
        state, payload = apply_kalshi_message(
            state,
            {
                "type": "orderbook_delta",
                "msg": {
                    "market_ticker": "KXMLBGAME-26AUG082010LADAZ-LAD",
                    "price": 40,
                    "delta": -100,
                    "side": "yes",
                },
            },
        )

        assert payload is not None
        ticker, book = payload
        assert ticker == "KXMLBGAME-26AUG082010LADAZ-LAD"
        # The 40c yes bid was fully consumed; 39c remains.
        assert book["orderbook"]["yes"] == [[39, 50]]
        assert book["orderbook"]["no"] == [[58, 200]]

    def test_the_payload_feeds_the_existing_normaliser(self) -> None:
        """Frame -> payload -> BookSnapshot: NO bids become derived YES asks."""
        state: KalshiBooks = {}
        state, payload = apply_kalshi_message(
            state,
            {
                "type": "orderbook_snapshot",
                "msg": {"market_ticker": "T", "yes": [[40, 100]], "no": [[58, 200]]},
            },
        )

        assert payload is not None
        snapshot = kalshi_snapshot(
            payload[1], contract_id=payload[0], received_time_ms=1_000
        )
        assert snapshot.best_ask is not None
        assert snapshot.best_ask.price == Decimal("0.42")
        assert snapshot.best_ask.size == 200

    def test_irrelevant_message_types_are_ignored(self) -> None:
        state: KalshiBooks = {}
        state, payload = apply_kalshi_message(state, {"type": "subscribed", "id": 1})

        assert payload is None
        assert state == {}

    def test_a_delta_before_any_snapshot_is_ignored(self) -> None:
        """Deltas against a book we never saw cannot be applied honestly."""
        state, payload = apply_kalshi_message(
            {},
            {
                "type": "orderbook_delta",
                "msg": {"market_ticker": "T", "price": 40, "delta": 5, "side": "yes"},
            },
        )

        assert payload is None


class TestPolymarketBookUpkeep:
    BOOK = {
        "event_type": "book",
        "asset_id": "token-1",
        "bids": [{"price": "0.55", "size": "100"}],
        "asks": [{"price": "0.60", "size": "80"}],
        "timestamp": "1754500000123",
    }

    def test_a_book_message_replaces_the_book(self) -> None:
        state: PolymarketBooks = {}
        state, payload = apply_polymarket_message(state, self.BOOK)

        assert payload is not None
        token, book = payload
        assert token == "token-1"
        assert book["asks"] == [{"price": "0.60", "size": "80"}]

    def test_a_price_change_updates_one_level(self) -> None:
        state: PolymarketBooks = {}
        state, _ = apply_polymarket_message(state, self.BOOK)
        state, payload = apply_polymarket_message(
            state,
            {
                "event_type": "price_change",
                "asset_id": "token-1",
                "changes": [
                    {"price": "0.60", "side": "SELL", "size": "0"},
                    {"price": "0.61", "side": "SELL", "size": "40"},
                ],
            },
        )

        assert payload is not None
        _, book = payload
        assert book["asks"] == [{"price": "0.61", "size": "40"}]

    def test_the_payload_feeds_the_existing_normaliser(self) -> None:
        state: PolymarketBooks = {}
        state, payload = apply_polymarket_message(state, self.BOOK)

        assert payload is not None
        snapshot = polymarket_snapshot(
            payload[1], contract_id=payload[0], received_time_ms=2_000
        )
        assert snapshot.best_bid is not None
        assert snapshot.best_bid.price == Decimal("0.55")
        assert snapshot.venue_time_ms == 1_754_500_000_123

    def test_a_change_for_an_unknown_token_is_ignored(self) -> None:
        state, payload = apply_polymarket_message(
            {},
            {
                "event_type": "price_change",
                "asset_id": "never-seen",
                "changes": [{"price": "0.5", "side": "BUY", "size": "10"}],
            },
        )

        assert payload is None


class TestKalshiRestLive:
    def test_fetches_real_open_mlb_markets(self) -> None:
        """Live against the public API; skips - not fails - when unreachable."""
        try:
            markets = fetch_mlb_markets()
        except (urllib.error.URLError, OSError) as error:
            pytest.skip(f"kalshi API unreachable: {error}")

        assert isinstance(markets, list)
        if markets:  # the season could, in principle, be over
            sample = markets[0]
            assert "ticker" in sample
            assert "event_ticker" in sample
            assert "rules_primary" in sample
