"""The collector daemon: propose candidates, collect books, start the clock.

    python3 -m arb.shell.collect

Three loops in one process:

* **Propose** (every 10 min): fetch open MLB markets from Kalshi and events
  from gamma, match games deterministically, extract each side's terms
  independently through the local LLM, and upsert new candidates. Existing
  candidates are never overwritten - an operator's decision from this morning
  survives the afternoon's cycle.
* **Reconcile** (every 30 s): re-read the candidate store's registry into the
  reducer's state and adjust websocket subscriptions. This is what makes an
  approval on the dashboard *start collection* without a restart.
* **Collect** (continuous): both venues' websockets feed frames through the
  pure book-upkeep functions into the ingestion pipeline; the reducer writes
  Decision Records - the verdict clock. A `Timer` fires every second so
  staleness and stuck-entry sweeps run.

Credentials: `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` are required -
startup fails naming the missing variable rather than degrading silently.
The LLM endpoint comes from `ARB_LLM_BASE_URL` (default vLLM's
`http://localhost:8000/v1`) and `ARB_LLM_MODEL` (required).

**One binding rule** keeps the whole chain connected: a candidate's kalshi
contract id is the *market ticker* and its polymarket contract id is the CLOB
*token id* - the identifiers websocket frames actually carry. The gamma
condition id appears nowhere downstream of matching, and books for the token
are the books for the pair's complementary outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from arb.config import Config
from arb.domain import MatchedPair
from arb.events import Timer
from arb.pricing import FeeSchedule
from arb.registry import propose, verify_candidate
from arb.shell.candidates import CandidateStore
from arb.shell.event_log import EventLog
from arb.shell.extract import ExtractorConfig, extract_terms
from arb.shell.ingest import BookMessage, IngestionPipeline
from arb.shell.kalshi_client import (
    KALSHI_WS_URL,
    apply_kalshi_message,
    fetch_mlb_markets,
    kalshi_ws_headers,
)
from arb.shell.kalshi_client import subscribe_command as kalshi_subscribe
from arb.shell.mlb_matcher import GamePairing, match_games
from arb.shell.polymarket_client import (
    POLYMARKET_WS_URL,
    apply_polymarket_message,
    fetch_mlb_events,
)
from arb.shell.polymarket_client import subscribe_command as polymarket_subscribe
from arb.shell.runtime import Runtime
from arb.shell.store import DecisionStore, OrderStore
from arb.state import State
from arb.verification import ContractTerms, KalshiSeries, PolymarketMarket

__all__ = ["main", "planned_subscriptions", "propose_once"]

logger = logging.getLogger("arb.collect")

DATA_DIR = Path("data/live_orderbooks")

#: Verified against both venues' current schedules (Aug 2026): Kalshi taker
#: 0.07, Polymarket sports taker theta 0.05 - which is exactly the spec's
#: worked example. Config, not constants; both venues changed rates this year.
SPORTS_FEES = FeeSchedule(kalshi_rate=Decimal("0.07"), polymarket_rate=Decimal("0.05"))

UNFETCHED = ContractTerms(
    settlement_source=None,
    settling_release=None,
    settling_release_timestamp=None,
    revisable=False,
    void_rule=None,
    postponement_rule=None,
    overtime_rule=None,
    threshold=None,
    tie_break_rule=None,
)


def collector_config() -> Config:
    """The thresholds agreed for the real run."""
    return Config(
        fee_schedules={"sports": SPORTS_FEES},
        min_net_edge=Decimal("0.005"),
        max_skew_ms=2_000,
        max_book_age_ms=5_000,
    )


def propose_once(
    store: CandidateStore,
    extractor: ExtractorConfig,
    *,
    kalshi_markets: Iterable[dict[str, Any]],
    gamma_events: Iterable[dict[str, Any]],
    now_ms: int,
) -> int:
    """One propose cycle over already-fetched listings. Returns new candidates.

    Each venue side is extracted in its own LLM call (independence - a model
    shown both texts harmonises them). A failed extraction leaves that side's
    terms unstated, so the candidate arrives unverifiable and fails closed
    with the verbatim prose still on the review card.
    """
    created = 0
    for pairing in match_games(kalshi_markets, gamma_events):
        if store.get(pairing.pair_id) is not None:
            continue  # never overwrite - decisions must survive re-proposal

        kalshi_text = _kalshi_rules_text(pairing, kalshi_markets)
        kalshi_extract = extract_terms(kalshi_text, extractor) if kalshi_text else None
        poly_extract = (
            extract_terms(pairing.polymarket_description, extractor)
            if pairing.polymarket_description
            else None
        )

        confidence = min(
            kalshi_extract.confidence if kalshi_extract else Decimal("0"),
            poly_extract.confidence if poly_extract else Decimal("0"),
        )

        candidate = propose(
            pair_id=pairing.pair_id,
            kalshi=KalshiSeries(
                # The market ticker, deliberately: it is the id Kalshi's
                # websocket frames carry, so it must be the registry's id too.
                series_ticker=pairing.kalshi_ticker,
                title=f"{pairing.kalshi_title} ({pairing.kalshi_team})",
                contract_terms_url=pairing.kalshi_event_url,
                terms=kalshi_extract.terms if kalshi_extract else UNFETCHED,
                resolution_text=kalshi_text,
                event_url=pairing.kalshi_event_url,
            ),
            polymarket=PolymarketMarket(
                # The CLOB token id, deliberately: books and subscriptions key
                # on tokens; the gamma condition id never appears in a frame.
                condition_id=pairing.polymarket_token_id,
                question=(
                    f"{pairing.polymarket_question} - "
                    f"{pairing.polymarket_outcome} side"
                ),
                resolution_source_url=pairing.polymarket_event_url,
                terms=poly_extract.terms if poly_extract else UNFETCHED,
                resolution_text=pairing.polymarket_description,
                event_url=pairing.polymarket_event_url,
            ),
            category="sports",
            settlement_date=pairing.game_date,
            model_confidence=confidence,
            proposed_at_ms=now_ms,
        )
        store.save(verify_candidate(candidate))
        created += 1

    if created:
        logger.info("propose cycle added %d candidates", created)
    return created


def planned_subscriptions(
    registry: Mapping[str, MatchedPair],
) -> tuple[frozenset[str], frozenset[str]]:
    """(kalshi tickers, polymarket tokens) the collector should be holding.

    Approved pairs only: approval is what starts collection, and an unapproved
    pair costs no bandwidth and writes no books.
    """
    return (
        frozenset(pair.kalshi_contract_id for pair in registry.values()),
        frozenset(pair.polymarket_contract_id for pair in registry.values()),
    )


def _kalshi_rules_text(
    pairing: GamePairing, markets: Iterable[dict[str, Any]]
) -> str:
    for market in markets:
        if market.get("ticker") == pairing.kalshi_ticker:
            primary = str(market.get("rules_primary") or "")
            secondary = str(market.get("rules_secondary") or "")
            return (primary + "\n\n" + secondary).strip()
    return ""


# --------------------------------------------------------------------------
# The async shell. Thin on purpose: everything above decides, this just runs.
# --------------------------------------------------------------------------


@dataclass
class _Shared:
    """State shared between the loops, mutated only on the event loop."""

    runtime: Runtime
    pipeline: IngestionPipeline
    kalshi_wanted: frozenset[str] = frozenset()
    polymarket_wanted: frozenset[str] = frozenset()


async def _run(store: CandidateStore, extractor: ExtractorConfig) -> None:
    runtime = Runtime(
        State(config=collector_config(), pair_registry=dict(store.registry())),
        decisions=DecisionStore(DATA_DIR / "decisions.sqlite"),
        orders=OrderStore(DATA_DIR / "orders.sqlite"),
        event_log=EventLog(DATA_DIR / "events.jsonl"),
    )
    shared = _Shared(runtime=runtime, pipeline=IngestionPipeline(runtime))

    await asyncio.gather(
        _propose_loop(store, extractor),
        _reconcile_loop(store, shared),
        _timer_loop(shared),
        _kalshi_ws_loop(shared),
        _polymarket_ws_loop(shared),
    )


async def _propose_loop(store: CandidateStore, extractor: ExtractorConfig) -> None:
    while True:
        try:
            markets, events = await asyncio.gather(
                asyncio.to_thread(fetch_mlb_markets),
                asyncio.to_thread(fetch_mlb_events),
            )
            await asyncio.to_thread(
                propose_once,
                store,
                extractor,
                kalshi_markets=markets,
                gamma_events=events,
                now_ms=int(time.time() * 1000),
            )
        except Exception:
            logger.exception("propose cycle failed; retrying next interval")
        await asyncio.sleep(600)


async def _reconcile_loop(store: CandidateStore, shared: _Shared) -> None:
    from dataclasses import replace

    while True:
        try:
            registry = dict(store.registry())
            if registry.keys() != shared.runtime.state.pair_registry.keys():
                logger.info("registry now %s", sorted(registry) or "empty")
            shared.runtime.state = replace(
                shared.runtime.state, pair_registry=registry
            )
            shared.kalshi_wanted, shared.polymarket_wanted = planned_subscriptions(
                registry
            )
        except Exception:
            logger.exception("registry reconcile failed")
        await asyncio.sleep(30)


async def _timer_loop(shared: _Shared) -> None:
    while True:
        shared.runtime.handle(Timer(int(time.time() * 1000)))
        await asyncio.sleep(1)


async def _kalshi_ws_loop(shared: _Shared) -> None:
    import websockets

    key_id = os.environ["KALSHI_API_KEY_ID"]
    pem = Path(os.environ["KALSHI_PRIVATE_KEY_PATH"]).expanduser().read_bytes()

    while True:
        subscribed: frozenset[str] = frozenset()
        books: dict[str, dict[str, dict[int, int]]] = {}
        try:
            headers = kalshi_ws_headers(
                key_id=key_id, private_key_pem=pem,
                timestamp_ms=int(time.time() * 1000),
            )
            async with websockets.connect(
                KALSHI_WS_URL, additional_headers=headers
            ) as ws:
                logger.info("kalshi websocket connected")
                command_id = 1
                while True:
                    if shared.kalshi_wanted != subscribed:
                        if shared.kalshi_wanted:
                            await ws.send(
                                kalshi_subscribe(
                                    sorted(shared.kalshi_wanted), command_id
                                )
                            )
                            command_id += 1
                        subscribed = shared.kalshi_wanted
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    frame = json.loads(raw)
                    books, payload = apply_kalshi_message(books, frame)
                    if payload is not None and payload[0] in subscribed:
                        shared.pipeline.accept(
                            BookMessage(
                                venue="kalshi",
                                contract_id=payload[0],
                                payload=payload[1],
                                received_time_ms=int(time.time() * 1000),
                            )
                        )
        except Exception as error:
            logger.warning("kalshi websocket dropped (%s); reconnecting in 5s", error)
            await asyncio.sleep(5)


async def _polymarket_ws_loop(shared: _Shared) -> None:
    import websockets

    while True:
        try:
            if not shared.polymarket_wanted:
                await asyncio.sleep(5)
                continue
            subscribed = shared.polymarket_wanted
            books: dict[str, dict[str, Any]] = {}
            async with websockets.connect(POLYMARKET_WS_URL) as ws:
                logger.info("polymarket websocket connected (%d tokens)", len(subscribed))
                await ws.send(polymarket_subscribe(sorted(subscribed)))
                while True:
                    # The public market channel has no resubscribe command; a
                    # changed token set reconnects with the new list.
                    if shared.polymarket_wanted != subscribed:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    parsed = json.loads(raw)
                    frames = parsed if isinstance(parsed, list) else [parsed]
                    for frame in frames:
                        if not isinstance(frame, dict):
                            continue
                        books, payload = apply_polymarket_message(books, frame)
                        if payload is not None and payload[0] in subscribed:
                            shared.pipeline.accept(
                                BookMessage(
                                    venue="polymarket",
                                    contract_id=payload[0],
                                    payload=payload[1],
                                    received_time_ms=int(time.time() * 1000),
                                )
                            )
        except Exception as error:
            logger.warning(
                "polymarket websocket dropped (%s); reconnecting in 5s", error
            )
            await asyncio.sleep(5)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    for var in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"):
        if not os.environ.get(var):
            sys.exit(f"error: {var} is not set - the Kalshi websocket requires it")
    key_path = Path(os.environ["KALSHI_PRIVATE_KEY_PATH"]).expanduser()
    if not key_path.exists():
        sys.exit(f"error: KALSHI_PRIVATE_KEY_PATH={key_path} does not exist")
    model = os.environ.get("ARB_LLM_MODEL")
    if not model:
        sys.exit("error: ARB_LLM_MODEL is not set - name the model vLLM is serving")

    extractor = ExtractorConfig(
        base_url=os.environ.get("ARB_LLM_BASE_URL", "http://localhost:8000/v1"),
        model=model,
    )
    store = CandidateStore(DATA_DIR / "pair_candidates.sqlite")
    logger.info(
        "collector starting: %d candidates, %d approved",
        len(store.all()),
        len(store.registry()),
    )
    try:
        asyncio.run(_run(store, extractor))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover - entry point
    main()
