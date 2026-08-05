"""Candidate persistence.

The approval workflow is worthless if it forgets. An operator who has to
re-review the same pair after a restart will stop reviewing carefully, and the
calibration dataset - model confidence against operator decision against
post-settlement truth - only exists if every stage survives the process that
wrote it.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from arb.registry import PairStatus, approve, reject, verify_candidate
from arb.shell.candidates import CandidateStore
from tests.test_registry import proposed
from tests.test_verification import market


class TestRoundTrip:
    def test_a_candidate_survives_a_restart(self, tmp_path: Path) -> None:
        CandidateStore(tmp_path / "pairs.sqlite").save(proposed())

        restored = CandidateStore(tmp_path / "pairs.sqlite").all()

        assert [c.pair_id for c in restored] == ["nfl-kc"]
        assert restored[0].model_confidence == Decimal("0.90")

    def test_the_contract_terms_survive_intact(self, tmp_path: Path) -> None:
        """The terms *are* the thing being reviewed. A lossy round trip would
        show the operator a different contract from the one that was verified."""
        store = CandidateStore(tmp_path / "pairs.sqlite")
        store.save(proposed())

        restored = store.all()[0]

        assert restored.kalshi.terms == proposed().kalshi.terms
        assert restored.polymarket.terms == proposed().polymarket.terms
        assert restored.kalshi.contract_terms_url == (
            "https://kalshi.com/terms/KXNFLGAME"
        )

    def test_an_unstated_term_stays_unstated(self, tmp_path: Path) -> None:
        """`None` must not come back as an empty string - that is the whole
        difference between "unverifiable" and "stated as blank"."""
        store = CandidateStore(tmp_path / "pairs.sqlite")
        store.save(replace(proposed(), polymarket=market(overtime_rule=None)))

        assert store.all()[0].polymarket.terms.overtime_rule is None

    def test_the_operator_decision_survives(self, tmp_path: Path) -> None:
        store = CandidateStore(tmp_path / "pairs.sqlite")
        decided = reject(
            verify_candidate(proposed()),
            operator="max",
            at_ms=2_000,
            note="different box score feed",
        )
        store.save(decided)

        restored = store.all()[0]
        assert restored.status is PairStatus.REJECTED_BY_OPERATOR
        assert restored.operator == "max"
        assert restored.operator_note == "different box score feed"
        assert restored.decided_at_ms == 2_000


class TestUpdates:
    def test_saving_the_same_pair_twice_updates_rather_than_duplicates(
        self, tmp_path: Path
    ) -> None:
        store = CandidateStore(tmp_path / "pairs.sqlite")
        store.save(verify_candidate(proposed()))

        store.save(approve(verify_candidate(proposed()), operator="max", at_ms=2_000))

        assert len(store.all()) == 1
        assert store.all()[0].status is PairStatus.APPROVED

    def test_a_single_pair_can_be_fetched_by_id(self, tmp_path: Path) -> None:
        store = CandidateStore(tmp_path / "pairs.sqlite")
        store.save(proposed())

        assert store.get("nfl-kc") is not None
        assert store.get("no-such-pair") is None

    def test_ground_truth_written_later_persists(self, tmp_path: Path) -> None:
        store = CandidateStore(tmp_path / "pairs.sqlite")
        candidate = approve(verify_candidate(proposed()), operator="max", at_ms=2_000)
        store.save(candidate)

        store.save(replace(candidate, settled_identically=False))

        restored = store.get("nfl-kc")
        assert restored is not None and restored.settled_identically is False

    def test_ground_truth_is_absent_until_settlement(self, tmp_path: Path) -> None:
        store = CandidateStore(tmp_path / "pairs.sqlite")
        store.save(proposed())

        restored = store.get("nfl-kc")
        assert restored is not None and restored.settled_identically is None


class TestTheRegistryItFeeds:
    def test_only_approved_pairs_reach_the_reducer(self, tmp_path: Path) -> None:
        store = CandidateStore(tmp_path / "pairs.sqlite")
        store.save(approve(verify_candidate(proposed()), operator="max", at_ms=2_000))
        store.save(
            verify_candidate(replace(proposed(), pair_id="pending-pair"))
        )

        assert list(store.registry()) == ["nfl-kc"]

    def test_an_empty_store_yields_an_empty_registry(self, tmp_path: Path) -> None:
        assert CandidateStore(tmp_path / "pairs.sqlite").registry() == {}


class TestResolutionTextPersists:
    def test_resolution_text_survives_the_round_trip(self, tmp_path: Path) -> None:
        store = CandidateStore(tmp_path / "pairs.sqlite")
        candidate = replace(
            proposed(),
            kalshi=replace(
                proposed().kalshi, resolution_text="Cancelled games resolve No."
            ),
        )
        store.save(candidate)

        restored = store.get("nfl-kc")

        assert restored is not None
        assert restored.kalshi.resolution_text == "Cancelled games resolve No."
        assert restored.polymarket.resolution_text == ""

    def test_a_store_created_before_the_column_existed_still_opens(
        self, tmp_path: Path
    ) -> None:
        """Schema migration: candidate DBs already exist on disk."""
        import sqlite3

        path = tmp_path / "pairs.sqlite"
        # Build the store, then strip the new columns to simulate an old file.
        CandidateStore(path)
        conn = sqlite3.connect(path)
        conn.executescript(
            "ALTER TABLE pair_candidates DROP COLUMN kalshi_resolution_text;"
            "ALTER TABLE pair_candidates DROP COLUMN polymarket_resolution_text;"
        )
        conn.commit()
        conn.close()

        store = CandidateStore(path)
        store.save(proposed())

        restored = store.get("nfl-kc")
        assert restored is not None and restored.kalshi.resolution_text == ""


class TestEventUrlsPersist:
    def test_event_urls_survive_the_round_trip(self, tmp_path: Path) -> None:
        store = CandidateStore(tmp_path / "pairs.sqlite")
        candidate = replace(
            proposed(),
            kalshi=replace(
                proposed().kalshi, event_url="https://kalshi.com/markets/KXNFL"
            ),
            polymarket=replace(
                proposed().polymarket,
                event_url="https://polymarket.com/event/chiefs",
            ),
        )
        store.save(candidate)

        restored = store.get("nfl-kc")

        assert restored is not None
        assert restored.kalshi.event_url == "https://kalshi.com/markets/KXNFL"
        assert restored.polymarket.event_url == "https://polymarket.com/event/chiefs"

    def test_a_store_without_the_event_columns_still_opens(
        self, tmp_path: Path
    ) -> None:
        import sqlite3

        path = tmp_path / "pairs.sqlite"
        CandidateStore(path)
        conn = sqlite3.connect(path)
        conn.executescript(
            "ALTER TABLE pair_candidates DROP COLUMN kalshi_event_url;"
            "ALTER TABLE pair_candidates DROP COLUMN polymarket_event_url;"
        )
        conn.commit()
        conn.close()

        store = CandidateStore(path)
        store.save(proposed())

        restored = store.get("nfl-kc")
        assert restored is not None and restored.kalshi.event_url == ""
