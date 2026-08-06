"""The collect daemon's decision-bearing parts, network-free.

The asyncio plumbing (reconnects, sleeps) is deliberately thin and untested;
what is tested is everything that decides: the propose cycle (fetch -> match ->
extract -> candidate), the id binding that lets websocket frames reach the
reducer, and the registry hot-reload that makes an approval start collection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from arb.registry import PairStatus, approve, verify_candidate
from arb.shell.candidates import CandidateStore
from arb.shell.collect import planned_subscriptions, propose_once
from arb.shell.extract import ExtractorConfig
from tests.test_extract import _FakeLLM

FIXTURES = Path(__file__).parent / "fixtures"

KALSHI_MARKETS = json.loads((FIXTURES / "kalshi_mlb_markets.json").read_text())[
    "markets"
]
GAMMA_EVENTS = json.loads((FIXTURES / "gamma_mlb_events.json").read_text())


@pytest.fixture()
def llm() -> Iterator[_FakeLLM]:
    fake = _FakeLLM()
    try:
        yield fake
    finally:
        fake.close()


# The scripted extractions return IDENTICAL term values for both venues - as a
# well-behaved model canonicalising cleanly-stated boilerplate would - but each
# side's evidence quotes ITS OWN text, because the extractor's hallucination
# guard checks quotes against the text of that call. Where a venue's text does
# not state a term (Kalshi never mentions extra innings; the sparse DET
# description states almost nothing), the value is null / the quote is absent,
# and verification fails closed exactly as it would on the real feed.
_TERMS = {
    "settlement_source": "official MLB game result",
    "settling_release": "final game result",
    "settling_release_timestamp": None,
    "revisable": False,
    "void_rule": "cancelled beyond two days resolves per rulebook",
    "postponement_rule": "market waits for the rescheduled game",
    "overtime_rule": None,
    "threshold": "team winning the game",
    "tie_break_rule": None,
}

_KALSHI_EVIDENCE = {
    "settlement_source": "professional baseball game",
    "settling_release": "the market resolves to Yes",
    "void_rule": "cancelled or rescheduled to over two days away",
    "postponement_rule": "postponed or delayed, the market will remain open",
    "threshold": "wins the",
}

_POLY_EVIDENCE = {
    "settlement_source": "the winner of the MLB game",
    "settling_release": "will resolve to the winner",
    "void_rule": "canceled entirely, with no make-up game",
    "postponement_rule": "If the game is postponed",
    "threshold": "resolve to the winner",
}


def kalshi_payload() -> str:
    return json.dumps(
        {"terms": _TERMS, "evidence": _KALSHI_EVIDENCE, "confidence": 0.9}
    )


def poly_payload() -> str:
    return json.dumps(
        {"terms": _TERMS, "evidence": _POLY_EVIDENCE, "confidence": 0.8}
    )


def queue_pairings(fake: _FakeLLM, count: int) -> None:
    """Pairings are processed in sorted order, Kalshi call then Polymarket."""
    for _ in range(count):
        fake.queue_content(kalshi_payload())
        fake.queue_content(poly_payload())


def run_propose(store: CandidateStore, fake: _FakeLLM) -> int:
    return propose_once(
        store,
        ExtractorConfig(base_url=fake.base_url, model="m", timeout_s=5),
        kalshi_markets=KALSHI_MARKETS,
        gamma_events=GAMMA_EVENTS,
        now_ms=1_754_500_000_000,
    )


class TestProposeOnce:
    def test_matched_games_become_stored_candidates(
        self, tmp_path: Path, llm: _FakeLLM
    ) -> None:
        store = CandidateStore(tmp_path / "pairs.sqlite")
        queue_pairings(llm, 4)

        created = run_propose(store, llm)

        assert created == 4
        assert {c.pair_id for c in store.all()} == {
            "kxmlbgame-26aug082010ladaz-lad",
            "kxmlbgame-26aug082010ladaz-az",
            "kxmlbgame-26aug081915detsf-det",
            "kxmlbgame-26aug081915detsf-sf",
        }

    def test_honest_extraction_verifies_rich_texts_and_fails_sparse_ones(
        self, tmp_path: Path, llm: _FakeLLM
    ) -> None:
        """The LAD descriptions state void and postponement handling, so both
        sides extract equal terms and the pairs await approval. The DET
        Polymarket description states almost nothing, so its quotes are not in
        the text, the fields null out, and the pairs fail closed - which is
        precisely what should happen to a thin real-world listing."""
        store = CandidateStore(tmp_path / "pairs.sqlite")
        queue_pairings(llm, 4)

        run_propose(store, llm)

        statuses = {c.pair_id: c.status for c in store.all()}
        assert statuses["kxmlbgame-26aug082010ladaz-lad"] is (
            PairStatus.AWAITING_APPROVAL
        )
        assert statuses["kxmlbgame-26aug082010ladaz-az"] is (
            PairStatus.AWAITING_APPROVAL
        )
        assert statuses["kxmlbgame-26aug081915detsf-det"] is (
            PairStatus.REJECTED_BY_RULES
        )
        assert statuses["kxmlbgame-26aug081915detsf-sf"] is (
            PairStatus.REJECTED_BY_RULES
        )

    def test_contract_ids_are_the_ids_books_arrive_under(
        self, tmp_path: Path, llm: _FakeLLM
    ) -> None:
        """The binding that makes collection work at all: the registry's
        kalshi id is the market ticker the websocket frames carry, and the
        polymarket id is the CLOB *token* - not the gamma condition id, which
        no book frame ever mentions."""
        store = CandidateStore(tmp_path / "pairs.sqlite")
        queue_pairings(llm, 4)
        run_propose(store, llm)

        candidate = store.get("kxmlbgame-26aug082010ladaz-lad")
        assert candidate is not None
        matched = approve(
            verify_candidate(candidate), operator="max", at_ms=1
        ).as_matched_pair()

        assert matched.kalshi_contract_id == "KXMLBGAME-26AUG082010LADAZ-LAD"
        assert matched.polymarket_contract_id == "22222222222222222222"

    def test_extraction_runs_once_per_venue_side(
        self, tmp_path: Path, llm: _FakeLLM
    ) -> None:
        """Independence again, now at the daemon level: 4 pairings over 2
        games = 8 extraction calls, each carrying exactly one venue's text."""
        store = CandidateStore(tmp_path / "pairs.sqlite")
        queue_pairings(llm, 4)

        run_propose(store, llm)

        assert len(llm.requests) == 8

    def test_a_failed_extraction_stores_an_unverifiable_candidate(
        self, tmp_path: Path, llm: _FakeLLM
    ) -> None:
        """LLM down mid-cycle: the pair still reaches the queue, fails closed,
        with the verbatim prose on the card. Nothing is silently dropped."""
        store = CandidateStore(tmp_path / "pairs.sqlite")
        # Sorted order: det, sf, az, lad. The first three pairings extract;
        # the queue then runs dry mid-cycle and the last call hits a 500.
        queue_pairings(llm, 3)

        created = run_propose(store, llm)

        assert created == 4
        statuses = {c.pair_id: c.status for c in store.all()}
        # az extracted cleanly on both sides -> awaiting. lad lost its kalshi
        # extraction to the dead endpoint -> unverifiable, but still queued.
        assert statuses["kxmlbgame-26aug082010ladaz-az"] is (
            PairStatus.AWAITING_APPROVAL
        )
        assert statuses["kxmlbgame-26aug082010ladaz-lad"] is (
            PairStatus.REJECTED_BY_RULES
        )

    def test_reproposing_does_not_overwrite_an_existing_decision(
        self, tmp_path: Path, llm: _FakeLLM
    ) -> None:
        """The propose cycle runs every ten minutes forever; an operator's
        approval from this morning must survive the afternoon's cycle."""
        store = CandidateStore(tmp_path / "pairs.sqlite")
        queue_pairings(llm, 4)
        run_propose(store, llm)
        candidate = store.get("kxmlbgame-26aug082010ladaz-lad")
        assert candidate is not None
        store.save(approve(verify_candidate(candidate), operator="max", at_ms=2))

        queue_pairings(llm, 4)
        created = run_propose(store, llm)

        assert created == 0
        again = store.get("kxmlbgame-26aug082010ladaz-lad")
        assert again is not None and again.status is PairStatus.APPROVED

    def test_the_candidate_carries_resolution_text_and_event_urls(
        self, tmp_path: Path, llm: _FakeLLM
    ) -> None:
        store = CandidateStore(tmp_path / "pairs.sqlite")
        queue_pairings(llm, 4)
        run_propose(store, llm)

        candidate = store.get("kxmlbgame-26aug082010ladaz-lad")
        assert candidate is not None
        assert "wins" in candidate.kalshi.resolution_text
        assert "resolve to the winner" in candidate.polymarket.resolution_text
        assert candidate.kalshi.event_url.startswith(
            "https://kalshi.com/markets/kxmlbgame/"
        )
        assert candidate.polymarket.event_url == (
            "https://polymarket.com/event/mlb-lad-az-2026-08-08"
        )


class TestSubscriptionPlanning:
    def test_only_approved_pairs_are_subscribed(
        self, tmp_path: Path, llm: _FakeLLM
    ) -> None:
        """Approval is what starts collection - unapproved pairs cost no
        bandwidth and write no books."""
        store = CandidateStore(tmp_path / "pairs.sqlite")
        queue_pairings(llm, 4)
        run_propose(store, llm)

        assert planned_subscriptions(store.registry()) == (frozenset(), frozenset())

        candidate = store.get("kxmlbgame-26aug082010ladaz-lad")
        assert candidate is not None
        store.save(approve(verify_candidate(candidate), operator="max", at_ms=1))

        kalshi, polymarket = planned_subscriptions(store.registry())
        assert kalshi == frozenset({"KXMLBGAME-26AUG082010LADAZ-LAD"})
        assert polymarket == frozenset({"22222222222222222222"})
