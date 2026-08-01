"""Evaluating one Matched Pair into one Decision Record.

Structured as an ordered chain of gates. Every gate produces a record - the
system must be able to say no, and to say *why* it said no - and the first gate
to fail names the reason. Order matters because the verdict report reads these
reasons as a funnel.
"""

from __future__ import annotations

from typing import Any, Callable

from arb.decisions import DecisionRecord, RejectionReason
from arb.domain import MatchedPair
from arb.pricing import fee_breakdown, gross_edge
from arb.risk import blocking_flags_for
from arb.sizing import walk
from arb.state import KillTier, State

__all__ = ["evaluate_pair"]

_Record = Callable[..., DecisionRecord]


def evaluate_pair(state: State, pair: MatchedPair, at_ms: int) -> DecisionRecord | None:
    """Evaluate one pair against the current books.

    Returns `None` when there is no candidate to judge at all - only one venue
    has ever sent a book for this pair. That is an absence of data, not a
    rejection, and recording it as one would pollute the denominator.
    """
    kalshi = state.books.get(pair.key_on("kalshi"))
    polymarket = state.books.get(pair.key_on("polymarket"))
    if kalshi is None or polymarket is None:
        return None

    # Freshness is a property of the snapshots, so it is measured before
    # anything else and attached to every record this evaluation produces -
    # including accepted ones, so a later reader can tell how fresh the books
    # behind a trade actually were.
    freshness: dict[str, Any] = {
        "skew_ms": abs(kalshi.received_time_ms - polymarket.received_time_ms),
        "kalshi_book_age_ms": max(0, state.now_ms - kalshi.received_time_ms),
        "polymarket_book_age_ms": max(0, state.now_ms - polymarket.received_time_ms),
    }
    record = _recorder(pair, at_ms, freshness)

    kalshi_ask = kalshi.best_ask
    polymarket_ask = polymarket.best_ask
    if kalshi_ask is None or polymarket_ask is None:
        return record(RejectionReason.EMPTY_BOOK)

    schedule = state.config.fees_for(pair.category)
    if schedule is None:
        return record(
            RejectionReason.UNPRICEABLE_CATEGORY,
            kalshi_price=kalshi_ask.price,
            polymarket_price=polymarket_ask.price,
        )

    # Any kill tier stops new entries. The tiers differ in what they do to
    # *open* positions, not in whether they stop opening new ones.
    if state.kill_tier is not KillTier.NONE:
        return record(
            RejectionReason.KILL_SWITCH,
            kalshi_price=kalshi_ask.price,
            polymarket_price=polymarket_ask.price,
        )

    fees = fee_breakdown(kalshi_ask.price, polymarket_ask.price, schedule)
    gross = gross_edge(kalshi_ask.price, polymarket_ask.price)
    net = gross - fees.total

    # Every gate below this point sees a fully priced candidate, so a rejection
    # still records the edge it was rejected on. That is what lets the analysis
    # later ask how much apparent edge each gate removed.
    priced: dict[str, Any] = {
        "kalshi_price": kalshi_ask.price,
        "polymarket_price": polymarket_ask.price,
        "gross_edge": gross,
        "net_edge": net,
        "fees": fees,
        "kalshi_top_size": kalshi_ask.size,
        "polymarket_top_size": polymarket_ask.size,
    }

    # Freshness gates sit after pricing so that a rejected candidate still
    # records the edge it appeared to have. That is what distinguishes a gate
    # that is protecting the system from one that is throwing away real money.
    #
    # Absolute age is checked before relative Skew: a book nobody has heard
    # from is a connectivity problem, and reporting it as mere desynchronisation
    # would hide the more serious fault.
    if max(
        freshness["kalshi_book_age_ms"], freshness["polymarket_book_age_ms"]
    ) > state.config.max_book_age_ms:
        return record(RejectionReason.STALE_BOOK, **priced)
    if freshness["skew_ms"] > state.config.max_skew_ms:
        return record(RejectionReason.EXCESSIVE_SKEW, **priced)

    if net <= 0:
        return record(RejectionReason.NEGATIVE_NET_EDGE, **priced)
    if net < state.config.min_net_edge:
        return record(RejectionReason.BELOW_MIN_NET_EDGE, **priced)

    # Sizing runs only once a candidate is worth sizing. The walk re-derives the
    # top-of-book edge as its first chunk, so reaching here guarantees a
    # non-zero size unless a budget caps it away.
    sized = walk(kalshi.asks, polymarket.asks, schedule)
    if not sized.is_tradeable:
        return record(RejectionReason.NO_PROFITABLE_SIZE, **priced)

    # Reading published flags and budgets - a handful of comparisons against
    # values the background pass already computed. No aggregation happens here.
    blocking = blocking_flags_for(
        pair,
        sized.kalshi_notional + sized.polymarket_notional,
        flags=state.risk_flags,
        budgets=state.risk_budgets,
        limits=state.config.risk,
    )
    if blocking:
        return record(
            RejectionReason.RISK_BLOCKED,
            **priced,
            blocking_flags=tuple(flag.value for flag in blocking),
        )

    return record(
        None,
        **priced,
        size=sized.size,
        expected_profit=sized.expected_profit,
    )


def _recorder(pair: MatchedPair, at_ms: int, always: dict[str, Any]) -> _Record:
    """A record builder bound to one pair at one moment.

    `always` holds fields every record carries regardless of which gate fired,
    so no gate has to remember to attach them.
    """

    def record(reason: RejectionReason | None, **fields: Any) -> DecisionRecord:
        return DecisionRecord(
            pair_id=pair.pair_id,
            category=pair.category,
            settlement_source=pair.settlement_source,
            settlement_date=pair.settlement_date,
            evaluated_at_ms=at_ms,
            accepted=reason is None,
            rejection_reason=reason,
            **always,
            **fields,
        )

    return record
