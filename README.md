# Kalshi <-> Polymarket Cross-Venue Arbitrage

A decision core built to answer one question: does a genuine, capturable
arbitrage exist between Kalshi and Polymarket? It prices every candidate
against the real, price-dependent fee curve on both venues, sizes to the point
where the marginal contract stops being profitable, and records every
evaluation - accepted **or** rejected - so a base rate has a denominator.

The earlier BTC 15-minute Up/Down collectors, dashboards and research
artifacts were removed; they answered a different question against a category
this strategy excludes by construction (the venues settle crypto on different
sources). They remain in git history.

## Layout

```text
arb/                                      # the decision core (stdlib only)
  reducer.py                              # the seam: step(State, Event)
  pricing.py sizing.py evaluate.py        # fees, Net Edge, marginal-stop walk
  verification.py registry.py review.py   # pair matching, approval, review
  risk.py inventory.py                    # flags, budgets, Drift steering
  legging.py execution.py exits.py        # order placement and recovery
  settlement.py report.py replay.py       # reconciliation and the verdict
  shell/                                  # persistence, runtime loop, review UI

tests/                                    # pytest suite - also the documentation
```

## Setup

Python 3.10+. No runtime dependencies.

```bash
python3 -m pip install -e .
python3 -m pip install pytest mypy   # development tooling
```

Verify:

```bash
python3 -m pytest
```

```bash
python3 -m mypy
```

## Run the collector

The collector proposes real MLB pairs and, once you approve one, collects its
order books - the verdict clock. It needs your Kalshi API key (websocket auth)
and a local LLM served by vLLM (term extraction):

```bash
export KALSHI_API_KEY_ID=your-key-id
export KALSHI_PRIVATE_KEY_PATH=~/kalshi-private-key.pem
export ARB_LLM_BASE_URL=http://localhost:8000/v1   # vLLM default
export ARB_LLM_MODEL=your-served-model-name
python3 -m arb.shell.collect
```

Every ~10 minutes it fetches both venues' MLB listings, matches games by
teams and date, extracts each side's terms independently through the LLM
(failed extraction fails closed - the pair arrives unverifiable with its
verbatim rules on the review card), and upserts the queue. Every 30 seconds
it re-reads the registry, so approving on the dashboard starts websocket
collection within half a minute. Books flow through the reducer; Decision
Records land in `data/live_orderbooks/decisions.sqlite`.

Fees are configured to both venues' verified August 2026 schedules (Kalshi
taker 0.07, Polymarket sports taker 0.05). Execution stays dry-run.

## The pair review dashboard

Candidate pairs are approved by a person, once per pair, after a deterministic
rule layer has compared both venues' resolution terms. The dashboard shows the
diff and takes the decision:

```bash
python3 -m arb.shell.review_server --operator YOUR_NAME
```

Then open http://127.0.0.1:8771. Approved pairs land in the registry the
reducer reads; a pair the rules rejected cannot be approved at all.

## Architecture

A functional core with an imperative shell. All logic sits behind one pure
reducer, `step(State, Event) -> (State, Action[])` - no clock, no I/O, no
randomness. Time and connectivity arrive as events, so a recorded event log
replays to a byte-identical action trace: the regression suite, the backtest,
and the evidence behind the verdict are the same artifact.

Two properties the whole design leans on:

- **Net Edge can be negative.** Fees are subtracted at each venue's real
  per-contract rate, `(0.07 + theta) * p * (1 - p)` - a parabola peaking at
  p=0.50, which is why the strategy is structurally viable only in the tails.
- **Rejections are persisted.** Every evaluated candidate is written with its
  fee breakdown and rejection reason, so "no opportunity existed" is
  distinguishable from "the system filtered it out".

Execution defaults to a dry run (`DryRunGateway` records order intent and
sends nothing). Live order routing requires supplying a real `OrderGateway`,
and should wait on the open items below.

## Still open

- Venue websocket adapters: nothing pulls live books yet. `BookSource` in
  `arb/shell/ingest.py` is the drop-in seam.
- Automatic cross-venue pair proposal (spec user story 9).
- Verdict criteria and capital allocation - both deliberately configuration,
  never defaulted. See `docs` in the spec worktree.

## Safety

- Not financial advice.
- Prediction-market execution has fill risk, latency risk, settlement risk,
  and venue-specific rule risk.
- Keep real money disabled until the decision log proves the strategy survives
  fees, slippage, stale data, and failed fills.
