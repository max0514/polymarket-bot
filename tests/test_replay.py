"""Deterministic replay - the highest-value test in the suite.

If a recorded event log replays to a byte-identical action trace, then a
changed trace means changed logic and nothing else. That is what makes the same
artifact serve as the regression suite, the backtest, and the evidence behind
the verdict.

Determinism has several distinct failure modes and each gets its own test:
a hidden clock, a random identifier, an unstable iteration order, and a
serialisation that spells the same number two ways.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from arb.actions import Action
from arb.events import (
    BalanceUpdate,
    BookUpdate,
    Event,
    Fill,
    KillSwitch,
    Settlement,
    Timer,
    TunnelHealth,
)
from arb.replay import action_trace, decode_event, encode_event, replay
from arb.state import State
from tests import builders as b


def scenario() -> tuple[State, list[Event]]:
    """One pair through a full life: quotes, entry, both fills, settlement."""
    matched = b.pair()
    other = b.pair("aaa-other-pair")
    state = b.state_with(matched, other)

    events: list[Event] = [
        BalanceUpdate("kalshi", Decimal("10000"), 1_000),
        BalanceUpdate("polymarket", Decimal("10000"), 1_000),
        TunnelHealth("kalshi", healthy=True, at_ms=1_000, latency_ms=120),
        TunnelHealth("polymarket", healthy=True, at_ms=1_000, latency_ms=80),
        BookUpdate(b.kalshi_book(other, asks=(("0.30", 200),), received_time_ms=1_100)),
        BookUpdate(
            b.polymarket_book(other, asks=(("0.69", 200),), received_time_ms=1_150)
        ),
        BookUpdate(
            b.kalshi_book(matched, asks=(("0.05", 100),), received_time_ms=1_200)
        ),
        BookUpdate(
            b.polymarket_book(matched, asks=(("0.90", 400),), received_time_ms=1_250)
        ),
        Timer(1_300),
    ]
    return state, events


def completed() -> tuple[State, list[Event]]:
    """The same scenario, carried through fills and settlement."""
    state, events = scenario()
    final, actions = replay(state, events)
    placed = [line for line in action_trace(actions) if line.startswith("order ")]
    leg_one_id = placed[0].split()[1]

    events = events + [
        Fill(leg_one_id, 100, Decimal("0.05"), 1_400),
    ]
    _, actions = replay(state, events)
    placed = [line for line in action_trace(actions) if line.startswith("order ")]
    leg_two_id = placed[1].split()[1]

    return state, events + [
        Fill(leg_two_id, 100, Decimal("0.90"), 1_500),
        Settlement(b.PAIR_ID, "kalshi", Decimal("1"), 9_000),
        Settlement(b.PAIR_ID, "polymarket", Decimal("0"), 9_100),
    ]


class TestDeterminism:
    def test_the_same_log_replays_to_the_same_trace(self) -> None:
        state, events = completed()

        _, first = replay(state, events)
        _, second = replay(state, events)

        assert action_trace(first) == action_trace(second)

    def test_the_trace_is_non_trivial(self) -> None:
        """A test that compares two empty traces proves nothing."""
        state, events = completed()
        _, actions = replay(state, events)
        trace = action_trace(actions)

        assert any(line.startswith("decision ") for line in trace)
        assert any(line.startswith("order ") for line in trace)
        assert any(line.startswith("settlement ") for line in trace)

    def test_replaying_from_a_serialised_log_gives_the_same_trace(self) -> None:
        """The log on disk, not the objects in memory, is what a backtest
        actually replays."""
        state, events = completed()

        lines = [encode_event(event) for event in events]
        restored = [decode_event(line) for line in lines]

        _, direct = replay(state, events)
        _, round_tripped = replay(state, restored)

        assert action_trace(round_tripped) == action_trace(direct)

    def test_encoding_is_stable_across_calls(self) -> None:
        state, events = completed()

        assert [encode_event(e) for e in events] == [encode_event(e) for e in events]

    def test_order_ids_do_not_vary_between_runs(self) -> None:
        """Minted from a state counter rather than generated, because a random
        id would change the trace on every run."""
        state, events = completed()

        _, first = replay(state, events)
        _, second = replay(state, events)

        def ids(actions: tuple[Action, ...]) -> list[str]:
            return [
                line.split()[1]
                for line in action_trace(actions)
                if line.startswith("order")
            ]

        assert ids(first) == ids(second)
        assert ids(first)  # and there were some

    def test_pair_iteration_order_does_not_depend_on_registry_insertion_order(
        self,
    ) -> None:
        """Two registries holding the same pairs in different orders must
        decide identically."""
        state, events = completed()
        reversed_registry = replace(
            state,
            pair_registry=dict(reversed(list(state.pair_registry.items()))),
        )

        _, forwards = replay(state, events)
        _, backwards = replay(reversed_registry, events)

        assert action_trace(backwards) == action_trace(forwards)


class TestReplayIsSensitiveToLogic:
    def test_a_different_configuration_produces_a_different_trace(self) -> None:
        """Configuration travels in state, so replaying under a different fee
        schedule is a different experiment - and visibly so."""
        state, events = completed()
        stricter = replace(
            state, config=replace(state.config, min_net_edge=Decimal("10"))
        )

        _, baseline = replay(state, events)
        _, tightened = replay(stricter, events)

        assert action_trace(tightened) != action_trace(baseline)

    def test_a_changed_event_log_produces_a_different_trace(self) -> None:
        state, events = completed()
        without_liquidity = [
            e
            for e in events
            if not (isinstance(e, BookUpdate) and e.snapshot.venue == "polymarket")
        ]

        _, full = replay(state, events)
        _, partial = replay(state, without_liquidity)

        assert action_trace(partial) != action_trace(full)


class TestReplayAsRegression:
    def test_the_recorded_trace_matches_the_current_logic(self) -> None:
        """The golden trace. When this fails, either the logic changed on
        purpose - update it and say why in the commit - or it changed by
        accident, which is the whole point.
        """
        state, events = completed()
        _, actions = replay(state, events)

        # The rejected pair, by hand: gross 1 - 0.30 - 0.69 = 0.01, against
        # fees 0.07*0.30*0.70 + 0.05*0.69*0.31 = 0.025395. Net -0.015395 - a
        # penny of apparent edge wiped out by a two-and-a-half-cent fee hurdle,
        # which is the whole thesis in one line.
        assert action_trace(actions) == (
            "decision aaa-other-pair reject negative_net_edge net=-0.01539500 size=0",
            "decision nfl-2026-w1-kc-win accept - net=0.04217500 size=100",
            "order nfl-2026-w1-kc-win:leg1:1 leg1 kalshi buy 100 @ 0.05000000",
            "order nfl-2026-w1-kc-win:leg2:2 leg2 polymarket buy 100 @ 0.90000000",
            "settlement nfl-2026-w1-kc-win realised=4.21750000 mismatch=0",
        )


class TestKillSwitchReplays:
    def test_a_kill_switch_in_the_log_replays_deterministically(self) -> None:
        state, events = completed()
        with_kill = events[:8] + [KillSwitch("stop_entries", 1_260)] + events[8:]

        _, first = replay(state, with_kill)
        _, second = replay(state, with_kill)

        assert action_trace(first) == action_trace(second)
