"""The reducer's state.

Immutable. Every transition returns a new `State` rather than mutating, so a
replayed event log cannot be perturbed by a caller holding a reference to an
earlier state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Mapping

from arb.actions import OrderPurpose
from arb.config import Config
from arb.domain import BookKey, BookSnapshot, MatchedPair, Venue, other_venue
from arb.risk import RiskBudgets, RiskFlag, evaluate_risk

if TYPE_CHECKING:  # pragma: no cover - import cycle: settlement imports state
    from arb.settlement import LegSettlement

__all__ = [
    "KillTier",
    "OrderRef",
    "PendingEntry",
    "Position",
    "State",
    "UnwindIncident",
]


class KillTier(Enum):
    """The kill switch, tiered so that the safety system does not itself
    destroy locked profit.

    Open pairs are the safest assets in the book - their exposure decays to
    zero at settlement - so flattening them is a loss-making action, reserved
    for account emergencies rather than used as the default response.
    """

    NONE = "none"
    #: L1 - stop new entries, hold open positions. The default.
    STOP_ENTRIES = "stop_entries"
    #: L2 - additionally exit positions flagged by a specific trigger.
    EXIT_FLAGGED = "exit_flagged"
    #: L3 - flatten everything.
    FLATTEN_ALL = "flatten_all"


@dataclass(frozen=True, slots=True)
class Position:
    """An open Matched Pair: both legs filled, held to settlement."""

    pair_id: str
    size: int
    kalshi_notional: Decimal
    polymarket_notional: Decimal
    category: str
    settlement_source: str
    settlement_date: str
    opened_at_ms: int

    #: Predicted Net Edge per contract at entry, kept so that realised profit
    #: can be reconciled against what the model promised.
    predicted_net_edge: Decimal = Decimal("0")

    #: Fees actually charged on entry, at the prices that actually filled.
    fees_paid: Decimal = Decimal("0")

    #: Set when an early-exit trigger fires. L2 exits exactly these.
    exit_trigger: str = ""

    #: Exit orders are already out. Prevents a second trigger, or a kill tier
    #: arriving after one, from selling the same position twice.
    exiting: bool = False

    #: Venues whose exit order has reported, filled or refused. The position is
    #: closed only when both have - half an exit is still a live pair, and an
    #: unbalanced one.
    exit_reported: frozenset[Venue] = frozenset()

    #: Cash recovered from exit fills so far.
    exit_proceeds: Decimal = Decimal("0")

    #: Fees charged on the exit legs.
    exit_fees: Decimal = Decimal("0")

    #: Venues whose exit order was refused, leaving that leg still held.
    exit_failed: frozenset[Venue] = frozenset()

    @property
    def notional(self) -> Decimal:
        """Capital committed across both venues - both legs are paid on entry."""
        return self.kalshi_notional + self.polymarket_notional

    @property
    def is_flagged(self) -> bool:
        return bool(self.exit_trigger)


@dataclass(frozen=True, slots=True)
class UnwindIncident:
    """What one Leg Failure actually cost, once the unwind filled."""

    pair_id: str
    size: int
    entry_price: Decimal
    exit_price: Decimal
    at_ms: int

    @property
    def cost(self) -> Decimal:
        """Positive when the unwind lost money, which is the usual case: it
        crosses the spread in the opposite direction to the entry."""
        return (self.entry_price - self.exit_price) * self.size


@dataclass(frozen=True, slots=True)
class OrderRef:
    """What an order was for, so a fill can be routed without the shell having
    to remember."""

    pair_id: str
    venue: Venue
    purpose: OrderPurpose


@dataclass(frozen=True, slots=True)
class PendingEntry:
    """A pair mid-entry: leg 1 placed or filled, leg 2 not yet done.

    This is the exposure window. It exists for exactly one round trip, and
    every path out of it either opens a Position or unwinds.
    """

    pair_id: str
    intended_size: int
    first_venue: Venue
    category: str
    settlement_source: str
    settlement_date: str
    predicted_net_edge: Decimal
    kalshi_limit: Decimal
    polymarket_limit: Decimal
    #: When leg 1 was placed. The only thing a timeout can be measured from.
    opened_at_ms: int = 0

    first_filled_size: int = 0
    first_fill_price: Decimal = Decimal("0")
    second_filled_size: int = 0
    second_fill_price: Decimal = Decimal("0")

    #: Leg 2 gets exactly one re-quote up to breakeven. Chasing further would
    #: turn a bounded recovery into an unbounded one.
    requoted: bool = False

    @property
    def notional(self) -> Decimal:
        """Capital this entry commits.

        Counted against the concentration budgets from the moment leg 1 is
        placed. Both legs are paid on entry, so an entry in flight has already
        committed its capital even though no Position exists yet.
        """
        return self.intended_size * (self.kalshi_limit + self.polymarket_limit)

    @property
    def second_venue(self) -> Venue:
        return other_venue(self.first_venue)

    def price_on(self, venue: Venue) -> Decimal:
        return self.kalshi_limit if venue == "kalshi" else self.polymarket_limit


@dataclass(frozen=True, slots=True)
class State:
    #: Configuration travels in state so a replay reproduces the original
    #: decisions rather than re-deciding under today's settings.
    config: Config = field(default_factory=Config)

    #: Latest book per (venue, contract).
    books: Mapping[BookKey, BookSnapshot] = field(default_factory=dict)

    #: The operator-approved set of Matched Pairs eligible for trading,
    #: published by the matching pipeline and read here as data.
    pair_registry: Mapping[str, MatchedPair] = field(default_factory=dict)

    positions: tuple[Position, ...] = ()

    #: Pairs mid-entry, keyed by pair id. A pair here is not a candidate.
    pending: Mapping[str, PendingEntry] = field(default_factory=dict)

    #: Live orders, keyed by order id, so a Fill can find what it belongs to.
    orders: Mapping[str, OrderRef] = field(default_factory=dict)

    #: Monotonic counter behind order ids. Ids must be derived from state
    #: rather than generated, because a random id would make replay produce a
    #: different action trace on every run.
    order_sequence: int = 0

    venue_balances: Mapping[Venue, Decimal] = field(default_factory=dict)
    venue_healthy: Mapping[Venue, bool] = field(default_factory=dict)
    venue_latency_ms: Mapping[Venue, int] = field(default_factory=dict)

    #: One entry per Leg Failure that reached the market, so the cost of a
    #: failure is measured per incident rather than only in aggregate.
    unwind_incidents: tuple[UnwindIncident, ...] = ()

    #: Legs that have settled, per pair, while the other leg has not. Holding
    #: them here is what makes asymmetric settlement timing observable rather
    #: than a race.
    settling: Mapping[str, Mapping[Venue, "LegSettlement"]] = field(
        default_factory=dict
    )

    #: Published by the background risk pass; read by the execution path.
    risk_flags: frozenset[RiskFlag] = frozenset()
    risk_budgets: RiskBudgets = field(default_factory=RiskBudgets)

    #: Cumulative Leg Failures, against the configured budget.
    leg_failures: int = 0

    kill_tier: KillTier = KillTier.NONE

    #: Latest time the reducer has been told about. Advances only through
    #: events - never read from a clock.
    now_ms: int = 0

    def with_book(self, snapshot: BookSnapshot) -> State:
        return replace(self, books={**self.books, snapshot.key: snapshot})

    def at_time(self, at_ms: int) -> State:
        """Advance the known time. Never moves backwards, so an out-of-order
        event cannot make a fresh book look stale."""
        return replace(self, now_ms=max(self.now_ms, at_ms))

    def with_republished_risk(self) -> State:
        """Re-run the background risk pass and publish the result.

        Called on every event that can change a risk input - balances,
        connectivity, positions, leg failures - and never from the path that
        evaluates a candidate.
        """
        flags, budgets = evaluate_risk(
            limits=self.config.risk,
            # Settled positions *and* entries in flight. Counting only the
            # former lets a burst of candidates each pass a budget that none of
            # them would pass together.
            positions=(*self.positions, *self.pending.values()),
            venue_balances=self.venue_balances,
            venue_healthy=self.venue_healthy,
            leg_failures=self.leg_failures,
        )
        return replace(self, risk_flags=flags, risk_budgets=budgets)

    def pairs_touching(self, key: BookKey) -> tuple[MatchedPair, ...]:
        """Registered pairs with a leg on this contract, in a stable order.

        Sorted by pair id because iteration order reaches the action trace, and
        an action trace that depends on dict insertion order is not replayable.
        """
        return tuple(
            self.pair_registry[pair_id]
            for pair_id in sorted(self.pair_registry)
            if key
            in (
                self.pair_registry[pair_id].key_on("kalshi"),
                self.pair_registry[pair_id].key_on("polymarket"),
            )
        )

    @property
    def unwind_cost(self) -> Decimal:
        """Cumulative cost of every Leg Failure so far."""
        return sum((incident.cost for incident in self.unwind_incidents), Decimal("0"))

    def position_for(self, pair_id: str) -> Position | None:
        for position in self.positions:
            if position.pair_id == pair_id:
                return position
        return None
