"""Contract-term extraction via a local LLM (vLLM, OpenAI-compatible).

The model is a *proposer*: it reads one venue's resolution prose and fills the
nine `ContractTerms` fields, with a confidence and a verbatim quote per field.
It carries no authority - the deterministic `verify()` still decides, and the
operator still approves.

Three rules keep it honest:

* **One venue per call.** The model never sees both texts together, because a
  model shown both will harmonise them and verification would then compare the
  model with itself.
* **Evidence or nothing.** A field survives only if its quote actually appears
  in the source text (case/whitespace-normalised). Hallucinated evidence
  drops the field to unstated, which fails closed at verification.
* **Anything unparseable is None.** Endpoint down, non-JSON reply, nonsense
  confidence - the candidate simply arrives unverifiable, with the verbatim
  prose still on the review card. Extraction can be retried; a wrong approval
  cannot.

Plain stdlib HTTP - the collector adds no SDK dependency for this.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from arb.verification import ContractTerms

__all__ = ["ExtractedTerms", "ExtractorConfig", "extract_terms"]

logger = logging.getLogger(__name__)

_TERM_FIELDS = (
    "settlement_source",
    "settling_release",
    "settling_release_timestamp",
    "void_rule",
    "postponement_rule",
    "overtime_rule",
    "threshold",
    "tie_break_rule",
)

_PROMPT = """You extract settlement terms from one prediction-market rules text.

Return ONLY a JSON object, no prose, with this exact shape:
{
  "terms": {
    "settlement_source": string or null,
    "settling_release": string or null,
    "settling_release_timestamp": string or null,
    "revisable": boolean,
    "void_rule": string or null,
    "postponement_rule": string or null,
    "overtime_rule": string or null,
    "threshold": string or null,
    "tie_break_rule": string or null
  },
  "evidence": { "<field name>": "<verbatim quote from the text>" },
  "confidence": number between 0 and 1
}

Rules:
- A field the text does not state MUST be null. Never guess or infer.
- Every non-null field MUST have an evidence entry quoting the text verbatim.
- "revisable" is true only if the text says the settling source publishes
  revisions or corrections.
- Confidence reflects how cleanly the text stated what you extracted.

TEXT:
"""


@dataclass(frozen=True, slots=True)
class ExtractorConfig:
    base_url: str
    model: str
    timeout_s: float = 30.0
    api_key: str = ""


@dataclass(frozen=True, slots=True)
class ExtractedTerms:
    terms: ContractTerms
    confidence: Decimal
    evidence: dict[str, str]


def extract_terms(text: str, config: ExtractorConfig) -> ExtractedTerms | None:
    """One venue's prose in, structured terms out. `None` on any failure."""
    content = _chat(_PROMPT + text, config)
    if content is None:
        return None

    payload = _parse_json(content)
    if payload is None:
        logger.warning("extractor returned non-JSON content; failing closed")
        return None

    try:
        raw_terms = dict(payload["terms"])
        evidence = {str(k): str(v) for k, v in dict(payload.get("evidence") or {}).items()}
        confidence = Decimal(str(payload["confidence"]))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        logger.warning("extractor payload missing required shape; failing closed")
        return None
    if not Decimal("0") <= confidence <= Decimal("1"):
        logger.warning("extractor confidence %s outside [0,1]; failing closed", confidence)
        return None

    # The hallucination guard: a field lives only on verbatim evidence.
    kept: dict[str, Any] = {}
    for field in _TERM_FIELDS:
        value = raw_terms.get(field)
        if value is None:
            kept[field] = None
            continue
        quote = evidence.get(field)
        if quote is None or not _appears_in(quote, text):
            logger.info("dropping %s: evidence quote not found in source", field)
            kept[field] = None
            continue
        kept[field] = str(value)

    return ExtractedTerms(
        terms=ContractTerms(revisable=bool(raw_terms.get("revisable", False)), **kept),
        confidence=confidence,
        evidence=evidence,
    )


def _chat(prompt: str, config: ExtractorConfig) -> str | None:
    """One chat-completions call. `None` on transport or protocol failure."""
    body = json.dumps(
        {
            "model": config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        config.base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            **(
                {"Authorization": f"Bearer {config.api_key}"}
                if config.api_key
                else {}
            ),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_s) as response:
            reply = json.loads(response.read().decode("utf-8"))
        content = reply["choices"][0]["message"]["content"]
        return str(content)
    except (
        urllib.error.URLError,
        OSError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
    ) as error:
        logger.warning("llm call failed (%s); failing closed", error)
        return None


def _parse_json(content: str) -> dict[str, Any] | None:
    """Parse the model's reply, tolerating a markdown code fence."""
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _appears_in(quote: str, text: str) -> bool:
    """Verbatim up to case and whitespace - drift there is not hallucination."""
    return _squash(quote) in _squash(text)


def _squash(value: str) -> str:
    return " ".join(value.split()).casefold()
