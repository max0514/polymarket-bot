"""The reducer - `step(State, Event) -> (State, Action[])`.

The single seam. All strategy, sizing, risk, and execution logic is reached
through here, which is why replay through this boundary is simultaneously the
test suite, the backtest, and the verdict instrument.

Pure: no clock, no I/O, no randomness. Time and connectivity arrive as events.
"""

from __future__ import annotations

from arb.actions import Action, EmitDecisionRecord
from arb.evaluate import evaluate_pair
from arb.events import BookUpdate, Event, Timer
from arb.state import State

__all__ = ["step"]


def step(state: State, event: Event) -> tuple[State, tuple[Action, ...]]:
    match event:
        case BookUpdate():
            return _on_book_update(state, event)
        case Timer():
            return state.at_time(event.at_ms), ()


def _on_book_update(state: State, event: BookUpdate) -> tuple[State, tuple[Action, ...]]:
    state = state.with_book(event.snapshot).at_time(event.at_ms)

    # Only pairs with a leg on the contract that just moved are re-evaluated.
    # Every other pair's inputs are unchanged, so re-pricing them would write
    # duplicate Decision Records and distort the denominator.
    records = [
        record
        for pair in state.pairs_touching(event.snapshot.key)
        if (record := evaluate_pair(state, pair, event.at_ms)) is not None
    ]
    return state, tuple(EmitDecisionRecord(record) for record in records)
