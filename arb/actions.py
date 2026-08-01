"""Actions - everything the reducer asks the shell to do.

The reducer returns these; it never performs them. `EmitDecisionRecord` is the
important one: that stream *is* the decision log the verdict is computed from.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, TypeAlias

from arb.decisions import DecisionRecord
from arb.domain import Venue

__all__ = [
    "Action",
    "Alert",
    "CancelOrder",
    "EmitDecisionRecord",
    "OrderPurpose",
    "PlaceOrder",
    "Side",
]

Side: TypeAlias = Literal["buy", "sell"]

#: Why an order exists. Carried on the order so that a fill can be routed
#: without the shell having to remember what it was asked to do.
OrderPurpose: TypeAlias = Literal["leg1", "leg2", "unwind", "exit"]


@dataclass(frozen=True, slots=True)
class EmitDecisionRecord:
    """Persist one evaluation, accepted or rejected."""

    record: DecisionRecord


@dataclass(frozen=True, slots=True)
class PlaceOrder:
    """Take liquidity. Both legs are taker orders.

    `limit_price` is a bound, not a target: for a buy it is the worst price
    accepted, and for the leg-2 order it is the breakeven implied by leg 1's
    actual fill.
    """

    order_id: str
    pair_id: str
    venue: Venue
    contract_id: str
    side: Side
    size: int
    limit_price: Decimal
    purpose: OrderPurpose


@dataclass(frozen=True, slots=True)
class CancelOrder:
    order_id: str
    venue: Venue


@dataclass(frozen=True, slots=True)
class Alert:
    """Reach the operator when they are not watching.

    Exists because several triggers in this system fire on discrete events that
    can happen overnight, and discovering them the following morning is the
    failure mode they were introduced to prevent.
    """

    severity: Literal["info", "warning", "critical"]
    message: str
    pair_id: str = ""


Action: TypeAlias = EmitDecisionRecord | PlaceOrder | CancelOrder | Alert
