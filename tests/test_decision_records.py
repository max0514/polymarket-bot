"""Decision Records - the denominator.

The system this replaces persisted only detected opportunities, so a base rate
could never be computed from it. The property under test throughout: an
evaluation is recorded whether or not it results in a trade.

Tests drive the reducer seam - `step(State, Event) -> (State, Action[])` - and
assert on the returned state and action trace only.
"""

from __future__ import annotations

from decimal import Decimal

from arb.decisions import DecisionRecord, RejectionReason
from arb.events import BookUpdate
from arb.reducer import step
from arb.state import State
from tests import builders as b
from tests.builders import records


def evaluate(
    kalshi_ask: str,
    polymarket_ask: str,
    *,
    min_net_edge: Decimal = Decimal("0.002"),
) -> tuple[list[DecisionRecord], State]:
    """Feed both sides of one pair in, return the Decision Records emitted."""
    matched = b.pair()
    state = b.state_with(matched, config_=b.config(min_net_edge=min_net_edge))

    state, first = step(
        state, BookUpdate(b.kalshi_book(matched, asks=((kalshi_ask, 500),)))
    )
    state, second = step(
        state, BookUpdate(b.polymarket_book(matched, asks=((polymarket_ask, 500),)))
    )
    return records(first) + records(second), state


class TestTheDenominator:
    def test_a_rejected_candidate_still_emits_a_decision_record(self) -> None:
        # 1c gross at the money: well under the ~3c fee hurdle.
        emitted, _ = evaluate("0.50", "0.49")

        assert len(emitted) == 1
        assert emitted[0].accepted is False
        assert emitted[0].rejection_reason is RejectionReason.NEGATIVE_NET_EDGE

    def test_an_accepted_candidate_emits_a_decision_record(self) -> None:
        emitted, _ = evaluate("0.05", "0.93")

        assert len(emitted) == 1
        assert emitted[0].accepted is True
        assert emitted[0].rejection_reason is None

    def test_a_positive_but_sub_threshold_edge_is_distinguishable_from_a_loss(
        self,
    ) -> None:
        """The funnel needs 'positive after fees' separable from 'traded'."""
        # gross 0.02 at p=0.05/0.93 nets ~0.0134; ask for more than that.
        emitted, _ = evaluate("0.05", "0.93", min_net_edge=Decimal("0.05"))

        assert emitted[0].accepted is False
        assert emitted[0].rejection_reason is RejectionReason.BELOW_MIN_NET_EDGE
        net = emitted[0].net_edge
        assert net is not None and net > 0

    def test_one_sided_book_updates_evaluate_nothing(self) -> None:
        """No opposing book means no candidate, not a rejected candidate."""
        matched = b.pair()
        _, actions = step(b.state_with(matched), BookUpdate(b.kalshi_book(matched)))

        assert records(actions) == []

    def test_a_book_update_for_an_unregistered_contract_evaluates_nothing(self) -> None:
        """Only operator-approved pairs are tradeable, so only they are priced."""
        matched = b.pair()
        _, actions = step(
            b.state_with(matched),
            BookUpdate(b.book("kalshi", "some-unapproved-contract")),
        )

        assert records(actions) == []


class TestWhatIsRecorded:
    def test_the_fee_breakdown_is_stored_per_candidate(self) -> None:
        """User story 3: re-run the analysis under a different fee schedule
        without recollecting data."""
        emitted, _ = evaluate("0.10", "0.88")

        fees = emitted[0].fees
        assert fees is not None
        assert fees.as_record() == {
            "kalshi_rate": "0.07000000",
            "polymarket_rate": "0.05000000",
            "kalshi_price": "0.10000000",
            "polymarket_price": "0.88000000",
            "kalshi_fee": "0.00630000",
            "polymarket_fee": "0.00528000",
            "total": "0.01158000",
        }

    def test_the_record_carries_both_prices_and_both_edges(self) -> None:
        emitted, _ = evaluate("0.10", "0.88")
        record = emitted[0]

        assert record.pair_id == b.PAIR_ID
        assert record.kalshi_price == Decimal("0.10")
        assert record.polymarket_price == Decimal("0.88")
        assert record.gross_edge == Decimal("0.02")
        assert record.net_edge == Decimal("0.00842")

    def test_the_record_is_serialisable_for_cross_pair_analysis(self) -> None:
        emitted, _ = evaluate("0.10", "0.88")
        row = emitted[0].as_record()

        assert row["pair_id"] == b.PAIR_ID
        assert row["accepted"] == "1"
        assert row["rejection_reason"] == ""
        assert row["net_edge"] == "0.00842000"
        assert row["category"] == "sports"
        assert row["settlement_source"] == "NFL official box score"


class TestSizedDecisions:
    def test_an_accepted_record_carries_the_size_the_walk_selected(self) -> None:
        matched = b.pair()
        state = b.state_with(matched)

        state, _ = step(
            state,
            BookUpdate(
                b.kalshi_book(matched, asks=(("0.05", 100), ("0.06", 100)))
            ),
        )
        state, actions = step(
            state,
            BookUpdate(
                b.polymarket_book(matched, asks=(("0.90", 100), ("0.935", 100)))
            ),
        )

        record = records(actions)[0]
        assert record.accepted is True
        # The second level loses after fees, so the walk stops at 100.
        assert record.size == 100
        assert record.expected_profit == Decimal("4.2175")

    def test_a_rejected_record_has_no_size(self) -> None:
        emitted, _ = evaluate("0.50", "0.49")

        assert emitted[0].size == 0
        assert emitted[0].expected_profit is None


class TestBookState:
    def test_the_reducer_keeps_the_latest_snapshot_per_contract(self) -> None:
        matched = b.pair()

        state, _ = step(
            b.state_with(matched),
            BookUpdate(b.kalshi_book(matched, asks=(("0.10", 500),))),
        )
        state, _ = step(
            state, BookUpdate(b.kalshi_book(matched, asks=(("0.11", 400),)))
        )

        book = state.books[("kalshi", matched.kalshi_contract_id)]
        assert book.asks[0].price == Decimal("0.11")

    def test_an_empty_book_side_is_not_a_tradeable_candidate(self) -> None:
        matched = b.pair()
        state = b.state_with(matched)

        state, _ = step(state, BookUpdate(b.kalshi_book(matched, asks=())))
        state, actions = step(
            state, BookUpdate(b.polymarket_book(matched, asks=(("0.88", 500),)))
        )

        emitted = records(actions)
        assert len(emitted) == 1
        assert emitted[0].rejection_reason is RejectionReason.EMPTY_BOOK
