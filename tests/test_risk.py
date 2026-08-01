"""Risk flags and budgets.

The architectural constraint is stronger than the individual limits: risk is
evaluated in the background and published as flags and budgets that the
execution path *reads*. No risk computation sits between the two legs, because
the exposure window is the one place in this system where latency converts
directly into loss.

So the tests assert two different things. That each limit blocks entry - and
that the flags are already on `State` before the book update that reads them,
rather than being computed when a candidate shows up.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from arb.decisions import DecisionRecord, RejectionReason
from arb.events import BalanceUpdate, BookUpdate, Timer, TunnelHealth
from arb.reducer import step
from arb.risk import RiskFlag, RiskLimits
from arb.state import Position, State
from tests import builders as b
from tests.builders import records


def position(
    pair_id: str = "other-pair",
    *,
    category: str = "sports",
    settlement_source: str = "NFL official box score",
    settlement_date: str = "2026-09-10",
    notional: str = "100",
) -> Position:
    half = Decimal(notional) / 2
    return Position(
        pair_id=pair_id,
        size=100,
        kalshi_notional=half,
        polymarket_notional=half,
        category=category,
        settlement_source=settlement_source,
        settlement_date=settlement_date,
        opened_at_ms=1_000,
    )


def entry_attempt(state: State) -> tuple[State, list[DecisionRecord]]:
    """Offer one clearly profitable candidate and see whether it is taken."""
    matched = b.pair()
    state, _ = step(state, BookUpdate(b.kalshi_book(matched, asks=(("0.05", 100),))))
    state, actions = step(
        state, BookUpdate(b.polymarket_book(matched, asks=(("0.90", 100),)))
    )
    return state, list(records(actions))


def healthy_state(
    *,
    limits: RiskLimits | None = None,
    positions: tuple[Position, ...] = (),
    kalshi_balance: str = "10000",
    polymarket_balance: str = "10000",
    leg_failures: int = 0,
) -> State:
    """A state where nothing is wrong yet, with risk already published."""
    state = b.state_with(
        b.pair(),
        config_=b.config(limits=limits),
        positions=positions,
        leg_failures=leg_failures,
    )
    state, _ = step(state, BalanceUpdate("kalshi", Decimal(kalshi_balance), 1_000))
    state, _ = step(
        state, BalanceUpdate("polymarket", Decimal(polymarket_balance), 1_000)
    )
    state, _ = step(state, TunnelHealth("kalshi", healthy=True, at_ms=1_000))
    state, _ = step(state, TunnelHealth("polymarket", healthy=True, at_ms=1_000))
    return state


class TestTheBaseline:
    def test_a_profitable_candidate_is_accepted_when_no_flag_is_raised(self) -> None:
        _, emitted = entry_attempt(healthy_state())

        assert emitted[0].accepted is True


class TestConnectivity:
    def test_a_degraded_venue_blocks_new_entries(self) -> None:
        """User story 45: never open a position that cannot be closed."""
        state = healthy_state()
        state, _ = step(state, TunnelHealth("polymarket", healthy=False, at_ms=2_000))

        _, emitted = entry_attempt(state)

        assert emitted[0].accepted is False
        assert emitted[0].rejection_reason is RejectionReason.RISK_BLOCKED
        assert "polymarket_connectivity_degraded" in emitted[0].blocking_flags

    def test_recovery_re_enables_entries(self) -> None:
        state = healthy_state()
        state, _ = step(state, TunnelHealth("kalshi", healthy=False, at_ms=2_000))
        state, _ = step(state, TunnelHealth("kalshi", healthy=True, at_ms=3_000))

        _, emitted = entry_attempt(state)

        assert emitted[0].accepted is True


class TestBalanceFloor:
    def test_a_balance_under_the_floor_blocks_entries_on_that_venue(self) -> None:
        """User story 33: one venue cannot be drained to a standstill."""
        state = healthy_state(
            limits=RiskLimits(balance_floor=Decimal("500")),
            kalshi_balance="400",
        )

        _, emitted = entry_attempt(state)

        assert emitted[0].rejection_reason is RejectionReason.RISK_BLOCKED
        assert "kalshi_balance_floor" in emitted[0].blocking_flags

    def test_a_balance_at_the_floor_is_allowed(self) -> None:
        state = healthy_state(
            limits=RiskLimits(balance_floor=Decimal("500")), kalshi_balance="500"
        )

        _, emitted = entry_attempt(state)

        assert emitted[0].accepted is True


class TestConcentration:
    def test_exposure_is_capped_per_settlement_source(self) -> None:
        """User story 47: many positions settling off one release are one risk,
        however many pairs they are spread across."""
        state = healthy_state(
            limits=RiskLimits(max_exposure_per_source=Decimal("150")),
            positions=(position(notional="100"),),
        )

        _, emitted = entry_attempt(state)

        assert emitted[0].rejection_reason is RejectionReason.RISK_BLOCKED
        assert "source_concentration" in emitted[0].blocking_flags

    def test_a_different_settlement_source_is_not_blocked(self) -> None:
        state = healthy_state(
            limits=RiskLimits(max_exposure_per_source=Decimal("150")),
            positions=(position(settlement_source="MLB official box score"),),
        )

        _, emitted = entry_attempt(state)

        assert emitted[0].accepted is True

    def test_exposure_is_capped_per_category(self) -> None:
        """User story 48: concentration visible along more than one axis."""
        state = healthy_state(
            limits=RiskLimits(max_exposure_per_category=Decimal("150")),
            positions=(position(settlement_source="MLB official box score"),),
        )

        _, emitted = entry_attempt(state)

        assert "category_concentration" in emitted[0].blocking_flags

    def test_exposure_is_capped_per_settlement_date(self) -> None:
        state = healthy_state(
            limits=RiskLimits(max_exposure_per_settlement_date=Decimal("150")),
            positions=(
                position(
                    category="economics", settlement_source="BLS CPI release"
                ),
            ),
        )

        _, emitted = entry_attempt(state)

        assert "settlement_date_concentration" in emitted[0].blocking_flags

    def test_total_unsettled_capital_is_capped(self) -> None:
        """User story 50: a slow or disputed settlement must not be able to
        lock the whole book."""
        state = healthy_state(
            limits=RiskLimits(max_unsettled_capital=Decimal("150")),
            positions=(
                position(
                    category="economics",
                    settlement_source="BLS CPI release",
                    settlement_date="2026-10-01",
                ),
            ),
        )

        _, emitted = entry_attempt(state)

        assert "unsettled_capital_cap" in emitted[0].blocking_flags


class TestInFlightCapital:
    """Capital committed to a pair mid-entry counts against the budgets.

    Both legs are paid on entry, so a pending entry has already committed its
    notional. Counting only settled positions lets a burst of candidates each
    pass a budget that none of them would pass together - which is exactly the
    concentration the budget exists to prevent.
    """

    def test_a_pending_entry_consumes_the_source_budget(self) -> None:
        state = healthy_state(
            limits=RiskLimits(max_exposure_per_source=Decimal("50"))
        )
        second = b.pair("bbb-second")
        state = replace(
            state,
            pair_registry={**state.pair_registry, second.pair_id: second},
        )

        # First candidate commits 50 * (0.05 + 0.90) = 47.50.
        state, _ = step(state, BookUpdate(b.kalshi_book(b.pair(), asks=(("0.05", 50),))))
        state, _ = step(
            state, BookUpdate(b.polymarket_book(b.pair(), asks=(("0.90", 50),)))
        )
        assert b.PAIR_ID in state.pending

        state, _ = step(state, BookUpdate(b.kalshi_book(second, asks=(("0.05", 50),))))
        state, actions = step(
            state, BookUpdate(b.polymarket_book(second, asks=(("0.90", 50),)))
        )

        emitted = records(actions)
        assert emitted[0].rejection_reason is RejectionReason.RISK_BLOCKED
        assert "source_concentration" in emitted[0].blocking_flags
        assert second.pair_id not in state.pending


class TestLegFailureBudget:
    def test_an_exhausted_leg_failure_budget_halts_entries(self) -> None:
        """User story 49: a systematic execution problem stops the system
        rather than bleeding it."""
        state = healthy_state(
            limits=RiskLimits(leg_failure_budget=3), leg_failures=3
        )

        _, emitted = entry_attempt(state)

        assert emitted[0].rejection_reason is RejectionReason.RISK_BLOCKED
        assert "leg_failure_budget" in emitted[0].blocking_flags

    def test_entries_continue_while_the_budget_holds(self) -> None:
        state = healthy_state(
            limits=RiskLimits(leg_failure_budget=3), leg_failures=2
        )

        _, emitted = entry_attempt(state)

        assert emitted[0].accepted is True


class TestRiskIsOffTheHotPath:
    def test_flags_are_published_on_state_before_any_candidate_appears(self) -> None:
        """The property the architecture turns on: by the time a book update
        arrives, the answer is already computed."""
        state = healthy_state(limits=RiskLimits(balance_floor=Decimal("500")))
        state, _ = step(state, BalanceUpdate("kalshi", Decimal("100"), 2_000))

        assert RiskFlag.KALSHI_BALANCE_FLOOR in state.risk_flags

    def test_a_timer_republishes_flags_without_emitting_decisions(self) -> None:
        """The background pass produces no Decision Records of its own - it
        would inflate the denominator with evaluations nobody asked for."""
        state = healthy_state()
        state, actions = step(state, Timer(9_000))

        assert actions == ()
        assert state.now_ms == 9_000

    def test_remaining_budgets_are_readable_rather_than_recomputed(self) -> None:
        """Published as budgets, not as a function the hot path has to call."""
        state = healthy_state(
            limits=RiskLimits(max_exposure_per_source=Decimal("500")),
            positions=(position(notional="100"),),
        )

        remaining = state.risk_budgets.per_source_remaining
        assert remaining["NFL official box score"] == Decimal("400")
