"""The verdict report - what the whole system exists to produce.

Three questions, in the order they have to be answered:

1. **Is there an edge at all?** The funnel counts every candidate evaluated,
   how many were positive after real fees, and how many were actually taken.
   A base rate needs the denominator, which is why rejections are recorded.

2. **Where is it?** Net Edge bucketed by the cheaper leg's price. The fee
   hurdle is a parabola peaking at 0.50, so the prediction is that this
   strategy is viable only in the tails. Bucketing is what confirms or refutes
   that, rather than leaving it as a plausible-sounding argument.

3. **Does it survive execution?** Predicted Net Edge against realised profit.
   A signal that cannot be filled is not a signal.

The report computes no verdict. The spec deliberately left the criteria open -
no threshold, observation window, or stopping rule was ever set - so declaring
a pass here would be inventing the standard the project is supposed to be
judged against. It reports the numbers; the operator sets the bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from arb.canonical import canonical_decimal
from arb.decisions import DecisionRecord
from arb.settlement import SettlementRecord

__all__ = ["Band", "BandStats", "ExecutionSummary", "Funnel", "Report", "build_report"]

ZERO = Decimal("0")

#: Boundaries on the *cheaper* leg's price. A matched pair has p1 + p2 ~= 1, so
#: the cheaper leg is in (0, 0.5] and locates the pair on the fee parabola.
_BAND_EDGES: tuple[tuple[str, Decimal], ...] = (
    ("0.00-0.05", Decimal("0.05")),
    ("0.05-0.10", Decimal("0.10")),
    ("0.10-0.25", Decimal("0.25")),
    ("0.25-0.50", Decimal("0.50")),
)

Band = str


@dataclass(frozen=True, slots=True)
class Funnel:
    """How many candidates survived each stage."""

    evaluated: int
    priced: int
    positive_after_fees: int
    accepted: int
    rejections: Mapping[str, int]

    @property
    def base_rate(self) -> Decimal:
        """Positive-after-fees as a share of everything evaluated.

        The number the old archive could not produce, because it kept only the
        numerator.
        """
        return _ratio(self.positive_after_fees, self.evaluated)

    @property
    def fill_rate(self) -> Decimal:
        """Accepted as a share of positive-after-fees. Everything lost here was
        removed by a gate, not by the market."""
        return _ratio(self.accepted, self.positive_after_fees)


@dataclass(frozen=True, slots=True)
class BandStats:
    band: Band
    evaluated: int
    positive_after_fees: int
    accepted: int
    expected_profit: Decimal

    @property
    def base_rate(self) -> Decimal:
        return _ratio(self.positive_after_fees, self.evaluated)


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    """Predicted against realised - how much of the paper edge survived."""

    pairs_settled: int
    mismatches: int
    predicted_profit: Decimal
    realised_profit: Decimal

    @property
    def mismatch_rate(self) -> Decimal:
        return _ratio(self.mismatches, self.pairs_settled)

    @property
    def realisation_ratio(self) -> Decimal:
        """Realised over predicted. One means the model was right on average;
        below one means the paper edge did not survive execution."""
        if self.predicted_profit == 0:
            return ZERO
        return self.realised_profit / self.predicted_profit


@dataclass(frozen=True, slots=True)
class Report:
    funnel: Funnel
    bands: Sequence[BandStats]
    execution: ExecutionSummary

    def render(self) -> str:
        lines = [
            "Kalshi <-> Polymarket arbitrage: standing verdict report",
            "",
            "Funnel",
            f"  candidates evaluated      {self.funnel.evaluated}",
            f"  priced                    {self.funnel.priced}",
            f"  positive after fees       {self.funnel.positive_after_fees}",
            f"  accepted                  {self.funnel.accepted}",
            f"  base rate                 {canonical_decimal(self.funnel.base_rate)}",
            "",
            "Rejections",
        ]
        lines.extend(
            f"  {reason:<25} {count}"
            for reason, count in sorted(self.funnel.rejections.items())
        )
        lines.extend(
            [
                "",
                "By cheaper-leg price (the tails-only prediction)",
                f"  {'band':<12}{'evaluated':>10}{'positive':>10}{'accepted':>10}"
                f"{'base rate':>14}",
            ]
        )
        lines.extend(
            f"  {band.band:<12}{band.evaluated:>10}{band.positive_after_fees:>10}"
            f"{band.accepted:>10}{canonical_decimal(band.base_rate):>14}"
            for band in self.bands
        )
        lines.extend(
            [
                "",
                "Execution",
                f"  pairs settled             {self.execution.pairs_settled}",
                f"  settlement mismatches     {self.execution.mismatches}",
                f"  predicted profit          "
                f"{canonical_decimal(self.execution.predicted_profit)}",
                f"  realised profit           "
                f"{canonical_decimal(self.execution.realised_profit)}",
                f"  realisation ratio         "
                f"{canonical_decimal(self.execution.realisation_ratio)}",
            ]
        )
        return "\n".join(lines)


def build_report(
    decisions: Iterable[DecisionRecord],
    settlements: Iterable[SettlementRecord] = (),
) -> Report:
    decisions = list(decisions)
    settlements = list(settlements)
    return Report(
        funnel=_funnel(decisions),
        bands=_bands(decisions),
        execution=_execution(settlements),
    )


def _funnel(decisions: Sequence[DecisionRecord]) -> Funnel:
    rejections: dict[str, int] = {}
    for decision in decisions:
        if decision.rejection_reason is not None:
            key = decision.rejection_reason.value
            rejections[key] = rejections.get(key, 0) + 1

    return Funnel(
        evaluated=len(decisions),
        priced=sum(1 for d in decisions if d.net_edge is not None),
        positive_after_fees=sum(
            1 for d in decisions if d.net_edge is not None and d.net_edge > 0
        ),
        accepted=sum(1 for d in decisions if d.accepted),
        rejections=rejections,
    )


def _bands(decisions: Sequence[DecisionRecord]) -> tuple[BandStats, ...]:
    buckets: dict[Band, list[DecisionRecord]] = {name: [] for name, _ in _BAND_EDGES}
    for decision in decisions:
        band = _band_for(decision)
        if band is not None:
            buckets[band].append(decision)

    return tuple(
        BandStats(
            band=name,
            evaluated=len(rows),
            positive_after_fees=sum(
                1 for d in rows if d.net_edge is not None and d.net_edge > 0
            ),
            accepted=sum(1 for d in rows if d.accepted),
            expected_profit=sum(
                (d.expected_profit for d in rows if d.expected_profit is not None),
                ZERO,
            ),
        )
        for name, rows in ((name, buckets[name]) for name, _ in _BAND_EDGES)
    )


def _band_for(decision: DecisionRecord) -> Band | None:
    """Locate a candidate on the fee parabola by its cheaper leg."""
    if decision.kalshi_price is None or decision.polymarket_price is None:
        return None
    cheaper = min(decision.kalshi_price, decision.polymarket_price)
    for name, upper in _BAND_EDGES:
        if cheaper < upper:
            return name
    # A pair whose cheaper leg is at or above 0.50 is at the money, where the
    # fee hurdle peaks. Reported in the last band rather than dropped.
    return _BAND_EDGES[-1][0]


def _execution(settlements: Sequence[SettlementRecord]) -> ExecutionSummary:
    return ExecutionSummary(
        pairs_settled=len(settlements),
        mismatches=sum(1 for s in settlements if s.mismatch),
        predicted_profit=sum((s.predicted_profit for s in settlements), ZERO),
        realised_profit=sum((s.realised_profit for s in settlements), ZERO),
    )


def _ratio(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return ZERO
    return Decimal(numerator) / Decimal(denominator)
