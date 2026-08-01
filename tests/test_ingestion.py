"""Live ingestion: the target universe, venue normalisation, and the shell loop.

Shell code, so it gets integration checks against recorded fixture shapes
rather than the exhaustive treatment the reducer gets. What is worth checking
here is exactly what the spec says is not tested at the reducer seam: that the
two venues' wire formats end up as the same book shape, and that the loop puts
each value where it belongs.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from arb.domain import Level
from arb.events import BookUpdate, Event, Settlement, Timer
from arb.registry import PairCandidate, approve, verify_candidate
from arb.shell.event_log import EventLog
from arb.shell.ingest import BookMessage, IngestionPipeline, RecordedSource
from arb.shell.normalise import kalshi_snapshot, polymarket_snapshot
from arb.shell.runtime import DryRunGateway, Runtime
from arb.shell.store import DecisionStore, OrderStore
from arb.shell.universe import Series, SeriesFilter, classify
from tests import builders as b
from tests.test_kill_switch import holding
from tests.test_registry import proposed

# Shapes taken from the venues' documented order book responses, matching what
# the existing collectors in `scripts/` already parse.
KALSHI_BOOK = {
    "orderbook": {
        "yes": [[4, 300], [3, 900]],
        "no": [[9, 250], [8, 400]],
    }
}

POLYMARKET_BOOK = {
    "bids": [{"price": "0.87", "size": "500"}, {"price": "0.86", "size": "900"}],
    "asks": [{"price": "0.90", "size": "400.6"}, {"price": "0.89", "size": "120"}],
    "timestamp": "1700000000123",
}


class TestKalshiNormalisation:
    def test_yes_asks_are_derived_from_no_bids(self) -> None:
        """Kalshi publishes only bids. A 9c bid for NO is a 91c offer of YES,
        and without deriving it the YES book has no ask side at all."""
        snapshot = kalshi_snapshot(
            KALSHI_BOOK, contract_id="KXNFL-KC", received_time_ms=1_000
        )

        assert snapshot.asks[0] == Level(Decimal("0.91"), 250)
        assert snapshot.asks[1] == Level(Decimal("0.92"), 400)

    def test_cents_become_decimal_dollars(self) -> None:
        snapshot = kalshi_snapshot(
            KALSHI_BOOK, contract_id="KXNFL-KC", received_time_ms=1_000
        )

        assert snapshot.best_bid == Level(Decimal("0.04"), 300)

    def test_bids_are_ordered_best_first(self) -> None:
        snapshot = kalshi_snapshot(
            KALSHI_BOOK, contract_id="KXNFL-KC", received_time_ms=1_000
        )

        assert [level.price for level in snapshot.bids] == [
            Decimal("0.04"),
            Decimal("0.03"),
        ]

    def test_the_receipt_time_is_stamped_by_us_not_the_venue(self) -> None:
        snapshot = kalshi_snapshot(
            KALSHI_BOOK, contract_id="KXNFL-KC", received_time_ms=1_234
        )

        assert snapshot.received_time_ms == 1_234

    def test_an_empty_book_normalises_rather_than_failing(self) -> None:
        snapshot = kalshi_snapshot(
            {"orderbook": {"yes": [], "no": []}},
            contract_id="KXNFL-KC",
            received_time_ms=1_000,
        )

        assert snapshot.asks == ()
        assert snapshot.best_ask is None


class TestPolymarketNormalisation:
    def test_asks_are_ordered_best_first(self) -> None:
        snapshot = polymarket_snapshot(
            POLYMARKET_BOOK, contract_id="0xabc", received_time_ms=1_000
        )

        assert [level.price for level in snapshot.asks] == [
            Decimal("0.89"),
            Decimal("0.90"),
        ]

    def test_fractional_share_sizes_are_floored_to_whole_contracts(self) -> None:
        """A partial share cannot be paired against a whole Kalshi contract,
        so rounding up would size a leg the book cannot fill."""
        snapshot = polymarket_snapshot(
            POLYMARKET_BOOK, contract_id="0xabc", received_time_ms=1_000
        )

        assert snapshot.asks[1].size == 400

    def test_the_venue_timestamp_is_kept_separately_from_receipt(self) -> None:
        snapshot = polymarket_snapshot(
            POLYMARKET_BOOK, contract_id="0xabc", received_time_ms=1_700_000_005_000
        )

        assert snapshot.venue_time_ms == 1_700_000_000_123
        assert snapshot.received_time_ms == 1_700_000_005_000

    def test_both_venues_produce_the_same_shape(self) -> None:
        kalshi = kalshi_snapshot(
            KALSHI_BOOK, contract_id="KXNFL-KC", received_time_ms=1_000
        )
        polymarket = polymarket_snapshot(
            POLYMARKET_BOOK, contract_id="0xabc", received_time_ms=1_000
        )

        for snapshot in (kalshi, polymarket):
            assert isinstance(snapshot.best_ask, Level)
            assert isinstance(snapshot.best_bid, Level)


class TestTargetUniverse:
    def test_sports_and_scheduled_economics_are_collected(self) -> None:
        assert classify("Will the Chiefs win the NFL game?") == "sports"
        assert classify("Will CPI come in above 3.0%?") == "economics"

    def test_crypto_is_excluded_by_construction(self) -> None:
        """Kalshi settles crypto on CF Benchmarks and Polymarket on Chainlink,
        so a crypto pair fails the matching rule before anyone prices it."""
        assert classify("Bitcoin above $100k?") == "crypto"
        assert not SeriesFilter().accepts(
            series_ticker="KXBTC", title="Bitcoin above $100k?"
        )

    def test_a_crypto_market_with_an_economic_trigger_is_still_crypto(self) -> None:
        assert classify("Bitcoin above $100k after the CPI print?") == "crypto"

    def test_parlays_are_filtered_before_they_flood_the_queue(self) -> None:
        """Open-market counts are dominated by combinatorial markets with no
        cross-venue equivalent."""
        assert not SeriesFilter().accepts(
            series_ticker="KXNFLPARLAY", title="NFL Sunday parlay: Chiefs & Eagles"
        )
        assert not SeriesFilter().accepts(
            series_ticker="KXNFLSCORE", title="Exact score: Chiefs 24 Eagles 21"
        )

    def test_an_explicitly_allowed_series_bypasses_the_heuristics(self) -> None:
        """The heuristics are a bandwidth filter; the Pair Registry is what
        actually gates trading, so the operator can always name a series."""
        allowing = SeriesFilter(allowed_series=frozenset({"KXODD"}))

        assert allowing.accepts(series_ticker="KXODD", title="Something unusual")

    def test_the_venues_own_category_beats_the_title_heuristic(self) -> None:
        """"Will the Chiefs win?" names no league, so keywords cannot place it.
        Both venues tag their series, and the tag is authoritative."""
        assert classify("Will the Chiefs win?") == "other"
        assert SeriesFilter().accepts(
            series_ticker="KXNFLGAME",
            title="Will the Chiefs win?",
            venue_category="Sports",
        )

    def test_a_venue_category_of_crypto_is_still_excluded(self) -> None:
        assert not SeriesFilter().accepts(
            series_ticker="KXSOMETHING",
            title="Will the price finish higher?",
            venue_category="Crypto",
        )

    def test_selection_preserves_input_order(self) -> None:
        selected = SeriesFilter().select(
            [
                Series("KXBTC", "Bitcoin above $100k?", "Crypto"),
                Series("KXNFL", "Will the Chiefs win?", "Sports"),
                Series("KXCPI", "Will CPI exceed 3%?"),
            ]
        )

        assert selected == ("KXNFL", "KXCPI")


class TestEventLog:
    def test_events_round_trip_through_the_log(self, tmp_path: Path) -> None:
        log = EventLog(tmp_path / "events.jsonl")
        matched = b.pair()
        events: list[Event] = [Timer(1_000), BookUpdate(b.kalshi_book(matched))]

        log.append_all(events)

        assert list(EventLog(tmp_path / "events.jsonl")) == events

    def test_the_log_appends_rather_than_replacing(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        EventLog(path).append(Timer(1_000))
        EventLog(path).append(Timer(2_000))

        assert list(EventLog(path)) == [Timer(1_000), Timer(2_000)]

    def test_reading_a_log_that_does_not_exist_yet_is_empty(self, tmp_path: Path) -> None:
        assert list(EventLog(tmp_path / "nothing.jsonl")) == []


class TestRuntime:
    def make(self, tmp_path: Path) -> Runtime:
        return Runtime(
            b.state_with(b.pair()),
            decisions=DecisionStore(tmp_path / "decisions.sqlite"),
            event_log=EventLog(tmp_path / "events.jsonl"),
            orders=OrderStore(tmp_path / "orders.sqlite"),
        )

    def test_every_order_attempt_is_persisted(self, tmp_path: Path) -> None:
        """User story 42: realised execution has to be reconcilable against
        intent, including intent that never became a fill."""
        runtime = self.make(tmp_path)
        matched = b.pair()

        runtime.handle(BookUpdate(b.kalshi_book(matched, asks=(("0.05", 100),))))
        runtime.handle(BookUpdate(b.polymarket_book(matched, asks=(("0.90", 100),))))

        rows = OrderStore(tmp_path / "orders.sqlite").all()
        assert [(row["purpose"], row["venue"], row["size"]) for row in rows] == [
            ("leg1", "kalshi", 100)
        ]
        assert rows[0]["limit_price"] == "0.05000000"

    def test_orders_are_not_sent_by_default(self, tmp_path: Path) -> None:
        """The default gateway records intent and sends nothing. Weeks of
        verdict data can be collected with no capital at risk."""
        runtime = self.make(tmp_path)
        matched = b.pair()

        runtime.handle(BookUpdate(b.kalshi_book(matched, asks=(("0.05", 100),))))
        runtime.handle(BookUpdate(b.polymarket_book(matched, asks=(("0.90", 100),))))

        gateway = runtime.gateway
        assert isinstance(gateway, DryRunGateway)
        assert [order.purpose for order in gateway.placed] == ["leg1"]

    def test_every_inbound_event_is_recorded_for_replay(self, tmp_path: Path) -> None:
        runtime = self.make(tmp_path)
        matched = b.pair()
        events = [
            BookUpdate(b.kalshi_book(matched, asks=(("0.05", 100),))),
            BookUpdate(b.polymarket_book(matched, asks=(("0.90", 100),))),
        ]

        runtime.handle_all(events)

        assert list(EventLog(tmp_path / "events.jsonl")) == events

    def test_the_pipeline_turns_raw_venue_books_into_decisions(
        self, tmp_path: Path
    ) -> None:
        """End to end through the shell: two venue payloads in, one Decision
        Record out."""
        runtime = self.make(tmp_path)
        matched = b.pair()
        pipeline = IngestionPipeline(runtime)

        accepted = pipeline.feed(
            RecordedSource(
                (
                    BookMessage(
                        "kalshi", matched.kalshi_contract_id, KALSHI_BOOK, 1_000
                    ),
                    BookMessage(
                        "polymarket",
                        matched.polymarket_contract_id,
                        POLYMARKET_BOOK,
                        1_000,
                    ),
                )
            )
        )

        assert accepted == 2
        rows = DecisionStore(tmp_path / "decisions.sqlite").all()
        assert len(rows) == 1

    def test_books_for_unregistered_contracts_never_reach_the_log(
        self, tmp_path: Path
    ) -> None:
        """The log's value is that it replays a decision, not that it archives
        a feed."""
        runtime = self.make(tmp_path)
        pipeline = IngestionPipeline(runtime)

        accepted = pipeline.feed(
            RecordedSource(
                (BookMessage("kalshi", "KXUNAPPROVED", KALSHI_BOOK, 1_000),)
            )
        )

        assert accepted == 0
        assert list(EventLog(tmp_path / "events.jsonl")) == []

    def test_decision_records_reach_the_store(self, tmp_path: Path) -> None:
        runtime = self.make(tmp_path)
        matched = b.pair()

        runtime.handle(BookUpdate(b.kalshi_book(matched, asks=(("0.50", 100),))))
        runtime.handle(BookUpdate(b.polymarket_book(matched, asks=(("0.49", 100),))))

        rows = DecisionStore(tmp_path / "decisions.sqlite").all()
        assert [row["rejection_reason"] for row in rows] == ["negative_net_edge"]


class TestCalibrationFeedback:
    """Settlement labels have to reach the Pair Registry automatically.

    The calibration dataset records what the model believed before the fact. It
    only becomes a calibration *curve* once something writes down what actually
    happened - and a label that depends on someone remembering to call a
    function will not be there in six months when the curve is wanted.
    """

    def runtime_holding(self, tmp_path: Path) -> tuple[Runtime, PairCandidate]:
        candidate = approve(verify_candidate(proposed()), operator="max", at_ms=2_000)
        state = holding()
        runtime = Runtime(
            state,
            decisions=DecisionStore(tmp_path / "decisions.sqlite"),
            event_log=EventLog(tmp_path / "events.jsonl"),
            candidates={b.PAIR_ID: replace(candidate, pair_id=b.PAIR_ID)},
        )
        return runtime, candidate

    def test_a_clean_settlement_labels_the_candidate(self, tmp_path: Path) -> None:
        runtime, _ = self.runtime_holding(tmp_path)

        runtime.handle(Settlement(b.PAIR_ID, "kalshi", Decimal("1"), 9_000))
        runtime.handle(Settlement(b.PAIR_ID, "polymarket", Decimal("0"), 9_000))

        assert runtime.candidates[b.PAIR_ID].settled_identically is True

    def test_a_mismatched_settlement_labels_the_candidate_negatively(
        self, tmp_path: Path
    ) -> None:
        runtime, _ = self.runtime_holding(tmp_path)

        runtime.handle(Settlement(b.PAIR_ID, "kalshi", Decimal("0"), 9_000))
        runtime.handle(Settlement(b.PAIR_ID, "polymarket", Decimal("0"), 9_000))

        labelled = runtime.candidates[b.PAIR_ID]
        assert labelled.settled_identically is False
        assert labelled.as_record()["model_confidence"] == "0.90000000"

    def test_a_single_leg_settling_labels_nothing_yet(self, tmp_path: Path) -> None:
        runtime, _ = self.runtime_holding(tmp_path)

        runtime.handle(Settlement(b.PAIR_ID, "kalshi", Decimal("1"), 9_000))

        assert runtime.candidates[b.PAIR_ID].settled_identically is None
