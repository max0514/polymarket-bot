"""Term extraction via a local LLM (vLLM, OpenAI-compatible).

Two properties carry the safety of the whole propose stage:

* **Independence.** Each venue's prose is extracted in its own call. A model
  shown both texts will quietly harmonise them, and then verification is
  comparing the model with itself rather than venue with venue.
* **Evidence or nothing.** Every extracted field must carry a verbatim quote
  from the source text, and the extractor checks the quote is actually a
  substring. A field whose evidence does not appear in the text is dropped to
  unstated - which fails closed into the unverifiable path.

The fake server below speaks just enough OpenAI chat-completions dialect to
exercise the client; no network leaves the test process.
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from arb.shell.extract import ExtractorConfig, extract_terms

RULES = (
    "If Los Angeles D wins the game scheduled for Aug 8, 2026, the market "
    "resolves to Yes. Extra innings count. If the game is cancelled or "
    "rescheduled to over two days away, the market resolves per the rulebook."
)


class _FakeLLM:
    """A scripted OpenAI-compatible endpoint."""

    def __init__(self) -> None:
        self.responses: list[Any] = []
        self.requests: list[dict[str, Any]] = []
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def queue_content(self, content: str) -> None:
        self.responses.append(
            {"choices": [{"message": {"role": "assistant", "content": content}}]}
        )

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                fake.requests.append(json.loads(self.rfile.read(length)))
                if not fake.responses:
                    self.send_response(500)
                    self.end_headers()
                    return
                body = json.dumps(fake.responses.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                pass

        return Handler


@pytest.fixture()
def llm() -> Iterator[_FakeLLM]:
    fake = _FakeLLM()
    try:
        yield fake
    finally:
        fake.close()


def config(fake: _FakeLLM) -> ExtractorConfig:
    return ExtractorConfig(base_url=fake.base_url, model="test-model", timeout_s=5)


def good_payload(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "terms": {
            "settlement_source": "MLB official game result",
            "settling_release": "Final score of the game",
            "settling_release_timestamp": None,
            "revisable": False,
            "void_rule": "Cancelled or rescheduled over two days resolves per the rulebook",
            "postponement_rule": None,
            "overtime_rule": "Extra innings count",
            "threshold": "Team winning the game",
            "tie_break_rule": None,
        },
        "evidence": {
            "overtime_rule": "Extra innings count",
            "void_rule": "cancelled or rescheduled to over two days away",
        },
        "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestExtraction:
    def test_a_clean_response_becomes_terms_with_confidence(
        self, llm: _FakeLLM
    ) -> None:
        llm.queue_content(good_payload())

        extracted = extract_terms(RULES, config(llm))

        assert extracted is not None
        assert extracted.terms.overtime_rule == "Extra innings count"
        assert extracted.terms.postponement_rule is None
        assert extracted.confidence == Decimal("0.9")

    def test_each_call_sees_exactly_one_venues_text(self, llm: _FakeLLM) -> None:
        """Independence is the point: the request must contain the text it was
        given and no mechanism for a second venue's text to ride along."""
        llm.queue_content(good_payload())

        extract_terms(RULES, config(llm))

        prompt = json.dumps(llm.requests[0])
        assert "Los Angeles D" in prompt
        assert llm.requests[0]["model"] == "test-model"

    def test_evidence_that_is_not_a_verbatim_quote_nulls_the_field(
        self, llm: _FakeLLM
    ) -> None:
        """The hallucination guard. The model claimed a quote about overtime
        that does not appear in the text, so the field is unstated - it will
        fail closed at verification rather than pass on invented evidence."""
        llm.queue_content(
            good_payload(
                evidence={
                    "overtime_rule": "Overtime is excluded entirely",
                    "void_rule": "cancelled or rescheduled to over two days away",
                }
            )
        )

        extracted = extract_terms(RULES, config(llm))

        assert extracted is not None
        assert extracted.terms.overtime_rule is None
        assert extracted.terms.void_rule is not None

    def test_a_field_without_any_evidence_is_nulled(self, llm: _FakeLLM) -> None:
        llm.queue_content(good_payload(evidence={}))

        extracted = extract_terms(RULES, config(llm))

        assert extracted is not None
        assert extracted.terms.overtime_rule is None
        assert extracted.terms.void_rule is None

    def test_evidence_matching_is_case_and_whitespace_tolerant(
        self, llm: _FakeLLM
    ) -> None:
        """Quotes come back with incidental case/spacing drift; that is not
        hallucination."""
        llm.queue_content(
            good_payload(
                evidence={
                    "overtime_rule": "extra  innings COUNT",
                    "void_rule": "cancelled or rescheduled to over two days away",
                }
            )
        )

        extracted = extract_terms(RULES, config(llm))

        assert extracted is not None
        assert extracted.terms.overtime_rule == "Extra innings count"


class TestFailsClosed:
    def test_endpoint_down_returns_none(self) -> None:
        dead = ExtractorConfig(
            base_url="http://127.0.0.1:1/v1", model="m", timeout_s=1
        )

        assert extract_terms(RULES, dead) is None

    def test_server_error_returns_none(self, llm: _FakeLLM) -> None:
        # No queued response -> the fake returns 500.
        assert extract_terms(RULES, config(llm)) is None

    def test_non_json_content_returns_none(self, llm: _FakeLLM) -> None:
        llm.queue_content("Sure! Here are the extracted terms:\n- overtime: counts")

        assert extract_terms(RULES, config(llm)) is None

    def test_json_wrapped_in_a_code_fence_still_parses(self, llm: _FakeLLM) -> None:
        """Local models love fences; a fence is formatting, not failure."""
        llm.queue_content("```json\n" + good_payload() + "\n```")

        extracted = extract_terms(RULES, config(llm))

        assert extracted is not None
        assert extracted.confidence == Decimal("0.9")

    def test_confidence_outside_zero_one_returns_none(self, llm: _FakeLLM) -> None:
        llm.queue_content(good_payload(confidence=1.7))

        assert extract_terms(RULES, config(llm)) is None
