"""The imperative shell's loop.

Takes an `Event`, records it, folds it through the reducer, and executes the
returned `Action`s. It contains no decisions - every branch here is about
*where a value goes*, never about what the value should be.

Order matters and is not arbitrary:

1. The event is written to the log **before** it is processed, so a crash
   mid-decision leaves a log that still replays to the same place.
2. Decision Records and settlements are persisted.
3. Orders go last, because everything above is evidence and an order is a
   commitment.

**Orders are not sent unless the gateway says so.** `DryRunGateway` is the
default and merely records intent. That matches the repository's existing rule
that live execution requires an explicit opt-in, and it means the verdict can
be collected for weeks with no capital at risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, MutableMapping

from arb.actions import (
    Action,
    Alert,
    CancelOrder,
    EmitDecisionRecord,
    EmitExitRecord,
    EmitSettlementRecord,
    PlaceOrder,
)
from arb.events import Event
from arb.exits import ExitRecord
from arb.reducer import step
from arb.registry import PairCandidate, record_ground_truth
from arb.settlement import SettlementRecord
from arb.shell.event_log import EventLog
from arb.shell.store import DecisionStore, OrderStore
from arb.state import State

__all__ = ["DryRunGateway", "OrderGateway", "Runtime"]


class OrderGateway:
    """Where orders go. Implementations talk to a venue; the default does not."""

    def place(self, order: PlaceOrder) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def cancel(self, cancel: CancelOrder) -> None:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class DryRunGateway(OrderGateway):
    """Records what would have been sent, and sends nothing.

    The default, deliberately. A verdict about whether an edge exists does not
    require the capital to be at risk while it is being collected, and the
    Decision Record log is complete either way.
    """

    placed: list[PlaceOrder] = field(default_factory=list)
    cancelled: list[CancelOrder] = field(default_factory=list)

    def place(self, order: PlaceOrder) -> None:
        self.placed.append(order)

    def cancel(self, cancel: CancelOrder) -> None:
        self.cancelled.append(cancel)


class Runtime:
    """One event in, side effects out."""

    def __init__(
        self,
        state: State,
        *,
        decisions: DecisionStore,
        event_log: EventLog,
        orders: OrderStore | None = None,
        gateway: OrderGateway | None = None,
        candidates: MutableMapping[str, PairCandidate] | None = None,
    ) -> None:
        self.state = state
        self._decisions = decisions
        self._orders = orders
        self._event_log = event_log
        self._gateway = gateway if gateway is not None else DryRunGateway()
        self.settlements: list[SettlementRecord] = []
        self.exits: list[ExitRecord] = []
        self.alerts: list[Alert] = []
        #: The calibration dataset. Ground truth is written here as settlements
        #: arrive, because a label that waits for someone to remember will not
        #: exist when the calibration curve is finally wanted.
        self.candidates: MutableMapping[str, PairCandidate] = (
            {} if candidates is None else candidates
        )

    @property
    def gateway(self) -> OrderGateway:
        return self._gateway

    def handle(self, event: Event) -> tuple[Action, ...]:
        # Written first: a crash between here and the end of this method must
        # still leave a log that replays to the same state.
        self._event_log.append(event)

        self.state, actions = step(self.state, event)

        self._decisions.append_all(
            action.record for action in actions if isinstance(action, EmitDecisionRecord)
        )
        if self._orders is not None:
            # Persisted whether or not the gateway sends them, so that intent
            # can be reconciled against what the venues actually did.
            self._orders.append_all(
                action for action in actions if isinstance(action, PlaceOrder)
            )

        for action in actions:
            match action:
                case EmitSettlementRecord():
                    self.settlements.append(action.record)
                    self._label(action.record)
                case EmitExitRecord():
                    self.exits.append(action.record)
                case Alert():
                    self.alerts.append(action)
                case PlaceOrder():
                    self._gateway.place(action)
                case CancelOrder():
                    self._gateway.cancel(action)
                case EmitDecisionRecord():
                    pass  # already persisted above, in one batch

        return actions

    def _label(self, record: SettlementRecord) -> None:
        """Write post-settlement ground truth back onto the candidate."""
        candidate = self.candidates.get(record.pair_id)
        if candidate is None:
            return
        self.candidates[record.pair_id] = record_ground_truth(
            candidate, settled_identically=not record.mismatch
        )

    def handle_all(self, events: Iterable[Event]) -> None:
        for event in events:
            self.handle(event)
