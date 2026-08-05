"""The review screen, driven over HTTP.

Shell code, so this is a handful of integration checks rather than exhaustive
coverage. What is worth checking is the behaviour that protects money: the
screen offers no override, and a decision reaches the store.

Driven through a real socket rather than by calling handler methods, because
the thing being tested is what an operator's browser can and cannot do.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from arb.registry import PairStatus, approve, verify_candidate
from arb.shell.candidates import CandidateStore
from arb.shell.review_server import build_handler, render_page
from arb.review import review_queue
from tests.test_registry import proposed
from tests.test_verification import market

FIXED_CLOCK = 1_700_000_000_000


@pytest.fixture()
def store(tmp_path: Path) -> CandidateStore:
    return CandidateStore(tmp_path / "pairs.sqlite")


@pytest.fixture()
def server(store: CandidateStore) -> Iterator[str]:
    handler = build_handler(store, "max", lambda: FIXED_CLOCK)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(httpd.server_address[1])
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def get(base: str, path: str = "/") -> str:
    with urllib.request.urlopen(base + path, timeout=5) as response:
        body: bytes = response.read()
    return body.decode("utf-8")


def post(base: str, path: str, **fields: str) -> int:
    body = "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in fields.items())
    request = urllib.request.Request(
        base + path, data=body.encode("utf-8"), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


class TestTheScreen:
    def test_an_empty_queue_says_so(self, server: str) -> None:
        assert "No candidates yet" in get(server)

    def test_a_candidate_shows_both_venues_terms(
        self, server: str, store: CandidateStore
    ) -> None:
        store.save(verify_candidate(proposed()))

        page = get(server)

        assert "nfl-kc" in page
        assert "KXNFLGAME" in page
        assert "Overtime counts toward the final score" in page
        assert "Void policy" in page

    def test_a_divergent_term_is_marked_on_the_page(
        self, server: str, store: CandidateStore
    ) -> None:
        store.save(
            verify_candidate(
                replace(proposed(), polymarket=market(void_rule="Void never"))
            )
        )

        page = get(server)

        assert 'class="differs"' in page
        assert "divergent void rule" in page

    def test_contract_sources_are_linked(
        self, server: str, store: CandidateStore
    ) -> None:
        store.save(verify_candidate(proposed()))

        page = get(server)

        assert 'href="https://kalshi.com/terms/KXNFLGAME"' in page
        assert 'href="https://polymarket.com/rules/0xabc"' in page


class TestDeciding:
    def test_approving_records_the_decision(
        self, server: str, store: CandidateStore
    ) -> None:
        store.save(verify_candidate(proposed()))

        assert post(server, "/approve", pair_id="nfl-kc") == 200

        decided = store.get("nfl-kc")
        assert decided is not None
        assert decided.status is PairStatus.APPROVED
        assert decided.operator == "max"
        assert decided.decided_at_ms == FIXED_CLOCK

    def test_an_approved_pair_reaches_the_registry_the_reducer_reads(
        self, server: str, store: CandidateStore
    ) -> None:
        store.save(verify_candidate(proposed()))
        assert store.registry() == {}

        post(server, "/approve", pair_id="nfl-kc")

        assert list(store.registry()) == ["nfl-kc"]

    def test_flagging_not_the_same_pair_records_the_reason(
        self, server: str, store: CandidateStore
    ) -> None:
        store.save(verify_candidate(proposed()))

        post(server, "/reject", pair_id="nfl-kc", note="Kalshi uses the league feed")

        decided = store.get("nfl-kc")
        assert decided is not None
        assert decided.status is PairStatus.REJECTED_BY_OPERATOR
        assert decided.operator_note == "Kalshi uses the league feed"

    def test_a_decided_pair_is_not_offered_again(
        self, server: str, store: CandidateStore
    ) -> None:
        store.save(verify_candidate(proposed()))
        post(server, "/approve", pair_id="nfl-kc")

        page = get(server)

        assert "Same pair &mdash; approve" not in page
        assert "Decided:" in page


class TestTheScreenOffersNoOverride:
    def test_a_rules_rejected_pair_has_no_approve_button(
        self, server: str, store: CandidateStore
    ) -> None:
        store.save(
            verify_candidate(
                replace(proposed(), polymarket=market(void_rule="Void never"))
            )
        )

        page = get(server)

        assert "Same pair &mdash; approve" not in page
        assert "The rule layer rejected this pair" in page

    def test_posting_an_approval_anyway_is_refused(
        self, server: str, store: CandidateStore
    ) -> None:
        """A crafted request must not get past the gate the screen hides."""
        store.save(
            verify_candidate(
                replace(proposed(), polymarket=market(void_rule="Void never"))
            )
        )

        assert post(server, "/approve", pair_id="nfl-kc") == 409

        still = store.get("nfl-kc")
        assert still is not None and still.status is PairStatus.REJECTED_BY_RULES

    def test_approving_a_pair_twice_is_refused(
        self, server: str, store: CandidateStore
    ) -> None:
        store.save(approve(verify_candidate(proposed()), operator="max", at_ms=1))

        assert post(server, "/approve", pair_id="nfl-kc") == 409

    def test_an_unknown_pair_is_a_404(self, server: str) -> None:
        assert post(server, "/approve", pair_id="no-such-pair") == 404


class TestRendering:
    def test_the_header_counts_what_is_waiting(self) -> None:
        views = list(
            review_queue(
                [
                    verify_candidate(proposed()),
                    approve(
                        verify_candidate(replace(proposed(), pair_id="done")),
                        operator="max",
                        at_ms=1,
                    ),
                ]
            )
        )

        page = render_page(views, "max")

        assert "1 awaiting" in page
        assert "2 total" in page

    def test_venue_text_is_escaped(self) -> None:
        """Contract terms are third-party text arriving from two venues. It is
        rendered into a page the operator trusts, so it is escaped."""
        hostile = replace(
            proposed(),
            polymarket=market(void_rule="<script>alert('x')</script>"),
        )

        page = render_page(list(review_queue([hostile])), "max")

        assert "<script>alert" not in page
        assert "&lt;script&gt;" in page


class TestResolutionOnThePage:
    def test_each_venues_resolution_text_is_shown(
        self, server: str, store: CandidateStore
    ) -> None:
        candidate = verify_candidate(proposed())
        candidate = replace(
            candidate,
            kalshi=replace(
                candidate.kalshi,
                resolution_text="If the game is cancelled, this market resolves No.",
            ),
            polymarket=replace(
                candidate.polymarket,
                resolution_text="Resolves to the official NFL result once final.",
            ),
        )
        store.save(candidate)

        page = get(server)

        assert "If the game is cancelled, this market resolves No." in page
        assert "Resolves to the official NFL result once final." in page

    def test_absent_resolution_text_shows_a_placeholder_not_nothing(
        self, server: str, store: CandidateStore
    ) -> None:
        """Silence must look like silence - an empty panel reads as a bug."""
        store.save(verify_candidate(proposed()))

        assert "No resolution text captured" in get(server)
