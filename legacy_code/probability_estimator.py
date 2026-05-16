"""Estimate event probabilities using Claude API or local LLMs (Ollama).

Supports two backends:
  - "claude" (default): Uses Anthropic Claude API
  - "ollama": Uses any Ollama model via its OpenAI-compatible API
    (e.g. qwen3.5:122b, llama3.3, mistral, deepseek-r1)

Set via --llm-provider CLI flag or LLM_PROVIDER env var.
"""

import json
import logging
import os
from datetime import datetime

import httpx

from .models import NewsArticle, Prediction
from .news_fetcher import format_articles_for_prompt

logger = logging.getLogger(__name__)

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-20250514"
DEFAULT_OLLAMA_MODEL = "qwen3.5:122b"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
TIMEOUT = 120.0  # local LLMs can be slow; 120s covers both
MAX_RETRIES = 3

# ── Prompts (shared across providers) ────────────────────────────────────────
SYSTEM_PROMPT = """You are a calibrated prediction analyst. You make probabilistic forecasts
that are well-calibrated: when you say 70%, events should resolve YES about 70% of the time.
You are aware of common cognitive biases and actively correct for them."""

ESTIMATION_PROMPT = """Analyze the following question and news evidence, then estimate the probability this event will occur.

QUESTION: {question}
RESOLUTION DATE: {end_date}
CURRENT MARKET PRICE (crowd estimate): {yes_price_pct}%

NEWS EVIDENCE:
{news_text}

Instructions:
1. Apply Bayesian reasoning: start with base rate, update on evidence
2. Consider: news recency, source reliability, black swan risk
3. Watch for: confirmation bias, narrative fallacies, fake/delayed news
4. Return a JSON object with:
   - estimated_probability: float (0.0 to 1.0)
   - confidence: "low" | "medium" | "high"
   - reasoning: string (3-5 sentences)
   - key_evidence: list of 3 most important facts
   - risks: list of 2-3 things that could make you wrong
   - bayesian_prior: float (what's the base rate before news?)

Return ONLY valid JSON, no other text."""


# ── Public API ───────────────────────────────────────────────────────────────


async def estimate_probability(
    market_id: str,
    question: str,
    end_date: datetime,
    yes_price: float,
    articles: list[NewsArticle],
    api_key: str = "",
    provider: str = "",
    model: str = "",
    ollama_url: str = "",
) -> Prediction:
    """Estimate probability using the configured LLM provider.

    Args:
        api_key: API key for Claude (ignored for ollama).
        provider: "claude" or "ollama". Falls back to LLM_PROVIDER env var,
                  then defaults to "claude".
        model: Model name override. Defaults per provider.
        ollama_url: Ollama server URL. Falls back to OLLAMA_URL env var.
    """
    # Resolve provider
    if not provider:
        provider = os.environ.get("LLM_PROVIDER", "claude").lower()

    news_text = format_articles_for_prompt(articles)
    prompt = ESTIMATION_PROMPT.format(
        question=question,
        end_date=end_date.strftime("%Y-%m-%d"),
        yes_price_pct=round(yes_price * 100, 1),
        news_text=news_text,
    )

    if provider == "ollama":
        return await _estimate_ollama(
            market_id=market_id,
            question=question,
            yes_price=yes_price,
            articles=articles,
            prompt=prompt,
            model=model or os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
            ollama_url=ollama_url or os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
        )
    else:
        return await _estimate_claude(
            market_id=market_id,
            question=question,
            yes_price=yes_price,
            articles=articles,
            prompt=prompt,
            api_key=api_key,
            model=model or os.environ.get("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
        )


# ── Claude backend ──────────────────────────────────────────────────────────


async def _estimate_claude(
    market_id: str,
    question: str,
    yes_price: float,
    articles: list[NewsArticle],
    prompt: str,
    api_key: str,
    model: str,
) -> Prediction:
    """Call Claude API (Anthropic SDK)."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                timeout=TIMEOUT,
            )

            text = response.content[0].text.strip()
            parsed = _parse_response(text)
            return _build_prediction(
                market_id, yes_price, articles, parsed, provider="claude", model=model,
            )

        except anthropic.APITimeoutError:
            last_error = f"Timeout on attempt {attempt}/{MAX_RETRIES}"
            logger.warning(last_error)
        except anthropic.APIError as e:
            last_error = f"API error on attempt {attempt}/{MAX_RETRIES}: {e}"
            logger.warning(last_error)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            last_error = f"Parse error on attempt {attempt}/{MAX_RETRIES}: {e}"
            logger.warning(last_error)

    return _fallback_prediction(market_id, yes_price, articles, last_error)


# ── Ollama backend (OpenAI-compatible API) ──────────────────────────────────


async def _estimate_ollama(
    market_id: str,
    question: str,
    yes_price: float,
    articles: list[NewsArticle],
    prompt: str,
    model: str,
    ollama_url: str,
) -> Prediction:
    """Call a local LLM via Ollama's OpenAI-compatible chat endpoint.

    Works with any model: qwen3.5:122b, llama3.3, mistral, deepseek-r1, etc.
    """
    url = f"{ollama_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,  # low temp for more calibrated outputs
        "max_tokens": 1024,
        "stream": False,
    }

    logger.info("Calling Ollama (%s) at %s for %s", model, ollama_url, market_id)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()

            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()

            # Some models wrap thinking in <think> tags — strip that
            text = _strip_thinking_tags(text)

            parsed = _parse_response(text)
            return _build_prediction(
                market_id, yes_price, articles, parsed, provider="ollama", model=model,
            )

        except httpx.ConnectError:
            last_error = (
                f"Cannot connect to Ollama at {ollama_url} "
                f"(attempt {attempt}/{MAX_RETRIES}). "
                f"Is Ollama running? Try: ollama serve"
            )
            logger.warning(last_error)
        except httpx.HTTPStatusError as e:
            last_error = f"Ollama HTTP error on attempt {attempt}/{MAX_RETRIES}: {e}"
            logger.warning(last_error)
            # If model not found, fail fast
            if e.response.status_code == 404:
                last_error = (
                    f"Model '{model}' not found in Ollama. "
                    f"Pull it first: ollama pull {model}"
                )
                logger.error(last_error)
                break
        except httpx.TimeoutException:
            last_error = f"Ollama timeout on attempt {attempt}/{MAX_RETRIES} (limit={TIMEOUT}s)"
            logger.warning(last_error)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            last_error = f"Parse error on attempt {attempt}/{MAX_RETRIES}: {e}"
            logger.warning(last_error)

    return _fallback_prediction(market_id, yes_price, articles, last_error)


# ── Shared helpers ──────────────────────────────────────────────────────────


def _strip_thinking_tags(text: str) -> str:
    """Strip <think>...</think> blocks that some models (DeepSeek, Qwen) emit."""
    import re
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _build_prediction(
    market_id: str,
    yes_price: float,
    articles: list[NewsArticle],
    parsed: dict,
    provider: str,
    model: str,
) -> Prediction:
    """Build a Prediction from parsed LLM response."""
    prediction = Prediction(
        market_id=market_id,
        timestamp=datetime.utcnow(),
        claude_probability=parsed["estimated_probability"],
        market_price=yes_price,
        edge=round(parsed["estimated_probability"] - yes_price, 4),
        confidence=parsed["confidence"],
        reasoning=f"[{provider}/{model}] {parsed['reasoning']}",
        bayesian_prior=parsed["bayesian_prior"],
        key_evidence=parsed.get("key_evidence", []),
        risks=parsed.get("risks", []),
        news_articles=[a.url for a in articles],
        news_quality_score=len(articles),
    )

    logger.info(
        "Estimated %s via %s/%s: prob=%.2f (market=%.2f, edge=%.2f) confidence=%s",
        market_id, provider, model,
        prediction.claude_probability,
        yes_price,
        prediction.edge,
        prediction.confidence,
    )
    return prediction


def _fallback_prediction(
    market_id: str,
    yes_price: float,
    articles: list[NewsArticle],
    last_error: str | None,
) -> Prediction:
    """Return a low-confidence fallback when all retries are exhausted."""
    logger.error("All %d attempts failed for %s: %s", MAX_RETRIES, market_id, last_error)
    return Prediction(
        market_id=market_id,
        timestamp=datetime.utcnow(),
        claude_probability=yes_price,  # Default to market consensus
        market_price=yes_price,
        edge=0.0,
        confidence="low",
        reasoning=f"Estimation failed after {MAX_RETRIES} attempts: {last_error}",
        bayesian_prior=yes_price,
        key_evidence=[],
        risks=["Estimation failed — using market consensus as fallback"],
        news_articles=[a.url for a in articles],
        news_quality_score=len(articles),
    )


def _parse_response(text: str) -> dict:
    """Parse and validate LLM JSON response."""
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    # Some models add trailing text after JSON — find the JSON object
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]

    data = json.loads(text)

    # Validate probability range
    prob = float(data["estimated_probability"])
    if not 0.0 <= prob <= 1.0:
        raise ValueError(f"Probability {prob} not in [0.0, 1.0]")
    data["estimated_probability"] = prob

    # Validate confidence
    conf = str(data["confidence"]).lower().strip()
    if conf not in ("low", "medium", "high"):
        raise ValueError(f"Invalid confidence: {data['confidence']}")
    data["confidence"] = conf

    # Validate bayesian_prior
    prior = float(data["bayesian_prior"])
    if not 0.0 <= prior <= 1.0:
        raise ValueError(f"Bayesian prior {prior} not in [0.0, 1.0]")
    data["bayesian_prior"] = prior

    return data
