"""Settlement reconciliation and mismatch detection.

Each leg settles on its own venue, on its own clock, and is recorded
independently - so asymmetric settlement timing is visible rather than averaged
away.

The reconciliation that matters is the sum. A correctly matched pair pays
exactly one dollar per contract in total: one leg wins, the other loses. Any
other total means the two venues disagreed about what happened, which is a
matching failure, not a trading loss. Absorbed quietly into P&L it would look
like variance; a strategy whose whole premise is that both venues settle
identically cannot afford to learn that lesson slowly.

The resulting labels flow back to the Pair Registry as the calibration
dataset's ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from arb.actions import Action, Alert, EmitSettlementRecord
from arb.canonical import canonical_decimal
from arb.domain import Venue
from arb.state import State

__all__ = ["LegSettlement", "SettlementRecord", "on_settlement"]

ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class LegSettlement:
    venue: Venue
    payout_per_contract: Decimal
    at_ms: int


@dataclass(frozen=True, slots=True)
class SettlementRecord:
    """One settled pair, reconciled against what was predicted."""

    pair_id: str
    size: int
    kalshi_payout: Decimal
    polymarket_payout: Decimal
    kalshi_settled_at_ms: int
    polymarket_settled_at_ms: int

    cost: Decimal
    fees_paid: Decimal
    realised_profit: Decimal
    predicted_profit: Decimal

    @property
    def total_payout_per_contract(self) -> Decimal:
        return self.kalshi_payout + self.polymarket_payout

    @property
    def mismatch(self) -> bool:
        """The legs settled differently.

        A matched pair pays exactly one dollar per contract across both venues.
        Anything else means the two venues resolved the same fact differently.
        """
        return self.total_payout_per_contract != ONE

    @property
    def settlement_skew_ms(self) -> int:
        """How far apart the two venues settled."""
        return abs(self.kalshi_settled_at_ms - self.polymarket_settled_at_ms)

    @property
    def model_error(self) -> Decimal:
        """Realised minus predicted. The continuous measure of model error."""
        return self.realised_profit - self.predicted_profit

    def as_record(self) -> dict[str, str]:
        return {
            "pair_id": self.pair_id,
            "size": str(self.size),
            "kalshi_payout": canonical_decimal(self.kalshi_payout),
            "polymarket_payout": canonical_decimal(self.polymarket_payout),
            "kalshi_settled_at_ms": str(self.kalshi_settled_at_ms),
            "polymarket_settled_at_ms": str(self.polymarket_settled_at_ms),
            "settlement_skew_ms": str(self.settlement_skew_ms),
            "cost": canonical_decimal(self.cost),
            "fees_paid": canonical_decimal(self.fees_paid),
            "realised_profit": canonical_decimal(self.realised_profit),
            "predicted_profit": canonical_decimal(self.predicted_profit),
            "model_error": canonical_decimal(self.model_error),
            "mismatch": "1" if self.mismatch else "0",
            "settled_identically": "0" if self.mismatch else "1",
        }


def on_settlement(
    state: State,
    pair_id: str,
    venue: Venue,
    payout_per_contract: Decimal,
    at_ms: int,
) -> tuple[State, tuple[Action, ...]]:
    """Record one leg's settlement; reconcile once both have arrived."""
    position = state.position_for(pair_id)
    if position is None:
        return state, ()

    legs = {
        **state.settling.get(pair_id, {}),
        venue: LegSettlement(venue, payout_per_contract, at_ms),
    }
    state = replace(state, settling={**state.settling, pair_id: legs})

    kalshi = legs.get("kalshi")
    polymarket = legs.get("polymarket")
    if kalshi is None or polymarket is None:
        # One venue has paid and the other has not. That is normal and is
        # exactly the asymmetry the timestamps exist to measure.
        return state, ()

    payout = (kalshi.payout_per_contract + polymarket.payout_per_contract) * position.size
    record = SettlementRecord(
        pair_id=pair_id,
        size=position.size,
        kalshi_payout=kalshi.payout_per_contract,
        polymarket_payout=polymarket.payout_per_contract,
        kalshi_settled_at_ms=kalshi.at_ms,
        polymarket_settled_at_ms=polymarket.at_ms,
        cost=position.notional,
        fees_paid=position.fees_paid,
        realised_profit=payout - position.notional - position.fees_paid,
        predicted_profit=position.predicted_net_edge * position.size,
    )

    state = replace(
        state,
        positions=tuple(p for p in state.positions if p.pair_id != pair_id),
        settling={k: v for k, v in state.settling.items() if k != pair_id},
    ).with_republished_risk()

    actions: list[Action] = [EmitSettlementRecord(record)]
    if record.mismatch:
        actions.append(
            Alert(
                severity="critical",
                message=(
                    f"settlement mismatch on {pair_id}: legs paid "
                    f"{record.kalshi_payout} and {record.polymarket_payout}"
                ),
                pair_id=pair_id,
            )
        )
    return state, tuple(actions)
