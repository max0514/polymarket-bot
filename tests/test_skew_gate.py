"""The Skew gate - stale books must not manufacture phantom edges.

Skew is measured on local receipt times, not venue clocks. The two venues do
not share a clock and neither is authoritative, so the only timestamp both
snapshots can be compared on is the one this process stamped them with. Venue
time is kept for measuring each venue's own lag, not for comparing venues.
"""

from __future__ import annotations

from decimal import Decimal

from arb.decisions import DecisionRecord, RejectionReason
from arb.events import BookUpdate
from arb.reducer import step
from tests import builders as b
from tests.builders import records


def evaluate_with_skew(
    *,
    kalshi_at: int,
    polymarket_at: int,
    max_skew_ms: int = 2_000,
    max_book_age_ms: int = 5_000,
    kalshi_ask: str = "0.05",
    polymarket_ask: str = "0.90",
) -> list[DecisionRecord]:
    """Both sides of one pair, arriving at the given local receipt times."""
    matched = b.pair()
    state = b.state_with(
        matched,
        config_=b.config(max_skew_ms=max_skew_ms, max_book_age_ms=max_book_age_ms),
    )
    state, _ = step(
        state,
        BookUpdate(
            b.kalshi_book(
                matched, asks=((kalshi_ask, 500),), received_time_ms=kalshi_at
            )
        ),
    )
    state, actions = step(
        state,
        BookUpdate(
            b.polymarket_book(
                matched, asks=((polymarket_ask, 500),), received_time_ms=polymarket_at
            )
        ),
    )
    return records(actions)


class TestSkewGate:
    def test_a_wide_edge_across_desynchronised_books_is_rejected(self) -> None:
        """The phantom edge: a 5c gross gap that exists only because one book
        is three seconds behind the other."""
        emitted = evaluate_with_skew(kalshi_at=1_000_000, polymarket_at=1_003_000)

        assert emitted[0].accepted is False
        assert emitted[0].rejection_reason is RejectionReason.EXCESSIVE_SKEW

    def test_the_same_edge_on_synchronised_books_is_accepted(self) -> None:
        """Same prices, same threshold - only the freshness differs."""
        emitted = evaluate_with_skew(kalshi_at=1_000_000, polymarket_at=1_000_500)

        assert emitted[0].accepted is True

    def test_skew_is_rejected_whichever_venue_is_behind(self) -> None:
        polymarket_late = evaluate_with_skew(
            kalshi_at=1_000_000, polymarket_at=1_003_000
        )
        kalshi_late = evaluate_with_skew(kalshi_at=1_003_000, polymarket_at=1_000_000)

        assert polymarket_late[0].rejection_reason is RejectionReason.EXCESSIVE_SKEW
        assert kalshi_late[0].rejection_reason is RejectionReason.EXCESSIVE_SKEW

    def test_skew_exactly_at_the_threshold_is_tolerated(self) -> None:
        emitted = evaluate_with_skew(
            kalshi_at=1_000_000, polymarket_at=1_002_000, max_skew_ms=2_000
        )

        assert emitted[0].accepted is True

    def test_the_rejected_candidate_records_the_edge_it_was_rejected_on(self) -> None:
        """A skew rejection is still evidence: it says how much apparent edge
        the gate removed, which is the only way to tell a working gate from an
        over-tight one."""
        emitted = evaluate_with_skew(kalshi_at=1_000_000, polymarket_at=1_003_000)
        record = emitted[0]

        assert record.skew_ms == 3_000
        assert record.gross_edge == Decimal("0.05")
        assert record.net_edge is not None and record.net_edge > 0


class TestStaleBooks:
    def test_a_book_older_than_the_age_threshold_rejects_the_candidate(self) -> None:
        emitted = evaluate_with_skew(
            kalshi_at=1_000_000,
            polymarket_at=1_007_000,
            max_skew_ms=60_000,  # isolate age from skew
            max_book_age_ms=5_000,
        )

        assert emitted[0].accepted is False
        assert emitted[0].rejection_reason is RejectionReason.STALE_BOOK

    def test_book_ages_are_recorded_for_both_venues(self) -> None:
        emitted = evaluate_with_skew(
            kalshi_at=1_000_000, polymarket_at=1_001_000, max_skew_ms=60_000
        )
        record = emitted[0]

        # Measured against the latest time the reducer has been told about,
        # which the polymarket update just advanced to 1_001_000.
        assert record.kalshi_book_age_ms == 1_000
        assert record.polymarket_book_age_ms == 0

    def test_time_only_advances_so_an_out_of_order_event_cannot_fake_staleness(
        self,
    ) -> None:
        """An event that arrives late must not rewind the clock and make a
        genuinely stale book look fresh."""
        matched = b.pair()
        state = b.state_with(matched, config_=b.config(max_skew_ms=60_000))

        state, _ = step(
            state,
            BookUpdate(b.kalshi_book(matched, received_time_ms=1_000_000)),
        )
        state, _ = step(
            state,
            BookUpdate(b.polymarket_book(matched, received_time_ms=1_020_000)),
        )
        # An older polymarket snapshot arrives out of order.
        state, actions = step(
            state,
            BookUpdate(b.polymarket_book(matched, received_time_ms=1_010_000)),
        )

        assert state.now_ms == 1_020_000
        assert records(actions)[0].rejection_reason is RejectionReason.STALE_BOOK
