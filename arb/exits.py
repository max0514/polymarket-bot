"""Early exit and the tiered kill switch.

Exiting a matched pair costs a second round of taker fees against a profit that
is already locked, and an open pair's exposure decays to zero at settlement by
itself. So nothing here fires on price. Every trigger is a discrete event that
means the pair has *stopped being a matched pair* - a dispute that moves
resolution to a token vote, a rule divergence that verification missed, or a
postponement that can void one leg and not the other.

The tiers exist so that the response is proportionate to the problem:

* **L1** stops entries and holds. The default, because the usual emergency is
  "something is wrong with the system", not "something is wrong with these
  positions".
* **L2** exits only what a trigger has flagged, so one bad market does not
  liquidate a whole book.
* **L3** flattens everything. Deliberately expensive, for account emergencies.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from arb.actions import Action, Alert, EmitExitRecord
from arb.canonical import canonical_decimal
from arb.domain import VENUES, Venue
from arb.legging import abandon_entry
from arb.orders import mint_order
from arb.pricing import FeeSchedule
from arb.state import KillTier, Position, State

__all__ = [
    "ExitRecord",
    "apply_kill_switch",
    "on_exit_fill",
    "on_exit_reject",
    "trigger_exit",
]

ONE = Decimal("1")
ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ExitRecord:
    """What one early exit actually cost.

    An early exit realises a loss against a profit that was already locked, so
    it has to be measured rather than assumed. Left unrecorded, the decision log
    would show the entry's predicted edge and never the price of abandoning it.
    """

    pair_id: str
    size: int
    trigger: str
    cost: Decimal
    proceeds: Decimal
    entry_fees: Decimal
    exit_fees: Decimal
    #: Venues whose exit was refused - those legs are still held.
    legs_unsold: tuple[Venue, ...]
    closed_at_ms: int

    @property
    def realised_profit(self) -> Decimal:
        return self.proceeds - self.cost - self.entry_fees - self.exit_fees

    def as_record(self) -> dict[str, str]:
        return {
            "pair_id": self.pair_id,
            "size": str(self.size),
            "trigger": self.trigger,
            "cost": canonical_decimal(self.cost),
            "proceeds": canonical_decimal(self.proceeds),
            "entry_fees": canonical_decimal(self.entry_fees),
            "exit_fees": canonical_decimal(self.exit_fees),
            "realised_profit": canonical_decimal(self.realised_profit),
            "legs_unsold": ",".join(self.legs_unsold),
            "closed_at_ms": str(self.closed_at_ms),
        }


def trigger_exit(
    state: State, pair_id: str, trigger: str, detail: str
) -> tuple[State, tuple[Action, ...]]:
    """Flag a position and close it.

    A trigger for a pair that is not held is not an error - the event stream
    covers every pair the operator watches, not only the ones with capital
    behind them.
    """
    # A pair mid-entry has no Position yet, but it is the most dangerous place
    # for a trigger to land: the system is holding an unhedged binary. Abandon
    # the entry rather than completing it into a market already known to be
    # broken.
    if pair_id in state.pending:
        state, actions = abandon_entry(state, pair_id, trigger)
        return state, actions

    position = state.position_for(pair_id)
    if position is None or position.exiting:
        return state, ()

    flagged = replace(position, exit_trigger=trigger)
    state = _replace_position(state, flagged)
    state, actions = _exit_position(state, flagged)

    return state, actions + (
        Alert(
            severity="critical",
            message=f"early exit on {pair_id}: {trigger} ({detail})".strip(" ()"),
            pair_id=pair_id,
        ),
    )


def apply_kill_switch(
    state: State, tier_name: str
) -> tuple[State, tuple[Action, ...]]:
    tier = KillTier(tier_name)
    state = replace(state, kill_tier=tier)

    if tier is KillTier.EXIT_FLAGGED:
        targets = [
            position
            for position in state.positions
            if position.is_flagged and not position.exiting
        ]
    elif tier is KillTier.FLATTEN_ALL:
        targets = [position for position in state.positions if not position.exiting]
    else:
        # L1 and clearing the switch touch no positions at all.
        targets = []

    actions: list[Action] = []
    for position in targets:
        # A position swept by a tier rather than by a trigger has no reason
        # recorded yet. The tier is the reason, and the exit record has to say
        # so - "why was this sold" is the first question anyone asks afterwards.
        if not position.is_flagged:
            position = replace(position, exit_trigger=tier.value)
            state = _replace_position(state, position)
        state, position_actions = _exit_position(state, position)
        actions.extend(position_actions)

    if tier is KillTier.FLATTEN_ALL and targets:
        actions.append(
            Alert(
                severity="critical",
                message=f"flattening {len(targets)} position(s)",
            )
        )
    return state, tuple(actions)


def on_exit_fill(
    state: State, pair_id: str, venue: Venue, size: int, price: Decimal, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """One exit leg sold. Close the position once both legs have reported."""
    position = state.position_for(pair_id)
    if position is None or venue in position.exit_reported:
        return state, ()

    schedule = state.config.fees_for(position.category)
    fee = (
        _venue_rate(schedule, venue) * price * (ONE - price) * size
        if schedule is not None
        else ZERO
    )
    position = replace(
        position,
        exit_reported=position.exit_reported | {venue},
        exit_proceeds=position.exit_proceeds + price * size,
        exit_fees=position.exit_fees + fee,
    )
    return _settle_exit(state, position, at_ms)


def on_exit_reject(
    state: State, pair_id: str, venue: Venue, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """One exit leg refused. The other leg is now unhedged."""
    position = state.position_for(pair_id)
    if position is None or venue in position.exit_reported:
        return state, ()

    position = replace(
        position,
        exit_reported=position.exit_reported | {venue},
        exit_failed=position.exit_failed | {venue},
    )
    state, actions = _settle_exit(state, position, at_ms)
    return state, actions + (
        Alert(
            severity="critical",
            message=(
                f"exit refused on {venue} for {pair_id}: that leg is still held "
                f"and is now naked"
            ),
            pair_id=pair_id,
        ),
    )


def _settle_exit(
    state: State, position: Position, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """Book the exit once every leg has reported; otherwise just record progress."""
    if len(position.exit_reported) < len(VENUES):
        return _replace_position(state, position), ()

    record = ExitRecord(
        pair_id=position.pair_id,
        size=position.size,
        trigger=position.exit_trigger,
        cost=position.notional,
        proceeds=position.exit_proceeds,
        entry_fees=position.fees_paid,
        exit_fees=position.exit_fees,
        legs_unsold=tuple(sorted(position.exit_failed)),
        closed_at_ms=at_ms,
    )
    state = replace(
        state,
        positions=tuple(p for p in state.positions if p.pair_id != position.pair_id),
    ).with_republished_risk()
    return state, (EmitExitRecord(record),)


def _venue_rate(schedule: FeeSchedule | None, venue: Venue) -> Decimal:
    if schedule is None:
        return ZERO
    return schedule.kalshi_rate if venue == "kalshi" else schedule.polymarket_rate


def _exit_position(state: State, position: Position) -> tuple[State, tuple[Action, ...]]:
    """Sell both legs into the bid.

    Both legs, because half-exiting a matched pair converts a hedged position
    into a naked one - the opposite of what every trigger here is trying to
    achieve.
    """
    pair = state.pair_registry.get(position.pair_id)
    if pair is None:
        return state, ()

    orders: list[Action] = []
    for venue in VENUES:
        book = state.books.get(pair.key_on(venue))
        bid = book.best_bid if book else None
        if bid is None:
            orders.append(
                Alert(
                    severity="critical",
                    message=f"no bid on {venue} to exit into",
                    pair_id=position.pair_id,
                )
            )
            continue

        state, order = mint_order(
            state,
            pair=pair,
            venue=venue,
            side="sell",
            size=position.size,
            limit_price=bid.price,
            purpose="exit",
        )
        orders.append(order)

    state = _replace_position(state, replace(position, exiting=True))
    return state, tuple(orders)


def _replace_position(state: State, position: Position) -> State:
    return replace(
        state,
        positions=tuple(
            position if existing.pair_id == position.pair_id else existing
            for existing in state.positions
        ),
    )
