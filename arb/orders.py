"""Minting orders.

One place, because entry, recovery, and exit all need the same four steps and
getting them subtly different is how an order id stops being reproducible.

Ids are derived from a counter on `State` rather than generated. A random or
time-based id would make a replayed action trace differ from the recorded one
on every run, which would cost the suite its only regression signal.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from arb.actions import OrderPurpose, PlaceOrder, Side
from arb.domain import MatchedPair, Venue
from arb.state import OrderRef, State

__all__ = ["mint_order"]


def mint_order(
    state: State,
    *,
    pair: MatchedPair,
    venue: Venue,
    side: Side,
    size: int,
    limit_price: Decimal,
    purpose: OrderPurpose,
) -> tuple[State, PlaceOrder]:
    """Return the state with the order registered, and the order to send."""
    sequence = state.order_sequence + 1
    order_id = f"{pair.pair_id}:{purpose}:{sequence}"
    order = PlaceOrder(
        order_id=order_id,
        pair_id=pair.pair_id,
        venue=venue,
        contract_id=pair.contract_on(venue),
        side=side,
        size=size,
        limit_price=limit_price,
        purpose=purpose,
    )
    state = replace(
        state,
        order_sequence=sequence,
        orders={
            **state.orders,
            order_id: OrderRef(pair_id=pair.pair_id, venue=venue, purpose=purpose),
        },
    )
    return state, order
