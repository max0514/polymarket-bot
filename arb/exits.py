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

from dataclasses import replace

from arb.actions import Action, Alert, PlaceOrder
from arb.domain import Venue
from arb.state import KillTier, OrderRef, Position, State

__all__ = ["apply_kill_switch", "trigger_exit"]

_VENUES: tuple[Venue, ...] = ("kalshi", "polymarket")


def trigger_exit(
    state: State, pair_id: str, trigger: str, detail: str, at_ms: int
) -> tuple[State, tuple[Action, ...]]:
    """Flag a position and close it.

    A trigger for a pair that is not held is not an error - the event stream
    covers every pair the operator watches, not only the ones with capital
    behind them.
    """
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
    state: State, tier_name: str, at_ms: int
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
    for venue in _VENUES:
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

        sequence = state.order_sequence + 1
        order_id = f"{position.pair_id}:exit:{sequence}"
        orders.append(
            PlaceOrder(
                order_id=order_id,
                pair_id=position.pair_id,
                venue=venue,
                contract_id=pair.contract_on(venue),
                side="sell",
                size=position.size,
                limit_price=bid.price,
                purpose="exit",
            )
        )
        state = replace(
            state,
            order_sequence=sequence,
            orders={
                **state.orders,
                order_id: OrderRef(
                    pair_id=position.pair_id, venue=venue, purpose="exit"
                ),
            },
        )

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
