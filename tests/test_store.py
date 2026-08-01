"""The Decision Record store.

Shell code, so it gets a small number of integration checks rather than the
exhaustive treatment the reducer gets. What is checked is the property the
verdict depends on: rejections reach the disk, not just acceptances.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from arb.decisions import DecisionRecord, RejectionReason
from arb.pricing import fee_breakdown
from arb.shell.store import DecisionStore
from tests.builders import SPORTS_FEES


def record(
    pair_id: str = "nfl-kc",
    *,
    accepted: bool = True,
    reason: RejectionReason | None = None,
    net: str = "0.00842",
) -> DecisionRecord:
    return DecisionRecord(
        pair_id=pair_id,
        category="sports",
        settlement_source="NFL official box score",
        settlement_date="2026-09-10",
        evaluated_at_ms=1_000_000,
        accepted=accepted,
        rejection_reason=reason,
        kalshi_price=Decimal("0.10"),
        polymarket_price=Decimal("0.88"),
        gross_edge=Decimal("0.02"),
        net_edge=Decimal(net),
        fees=fee_breakdown(Decimal("0.10"), Decimal("0.88"), SPORTS_FEES),
    )


class TestDecisionStore:
    def test_persists_rejections_as_well_as_acceptances(self, tmp_path: Path) -> None:
        """The denominator property, end to end."""
        store = DecisionStore(tmp_path / "decisions.sqlite")
        store.append(record("accepted-pair"))
        store.append(
            record(
                "rejected-pair",
                accepted=False,
                reason=RejectionReason.NEGATIVE_NET_EDGE,
            )
        )

        rows = store.all()
        assert [row["pair_id"] for row in rows] == ["accepted-pair", "rejected-pair"]
        assert [row["rejection_reason"] for row in rows] == ["", "negative_net_edge"]

    def test_stores_the_fee_breakdown_for_later_reanalysis(self, tmp_path: Path) -> None:
        store = DecisionStore(tmp_path / "decisions.sqlite")
        store.append(record())

        row = store.all()[0]
        assert row["fee_kalshi_fee"] == "0.00630000"
        assert row["fee_polymarket_fee"] == "0.00528000"
        assert row["fee_total"] == "0.01158000"

    def test_reopening_the_store_keeps_earlier_records(self, tmp_path: Path) -> None:
        path = tmp_path / "decisions.sqlite"
        DecisionStore(path).append(record("first"))
        DecisionStore(path).append(record("second"))

        assert [row["pair_id"] for row in DecisionStore(path).all()] == [
            "first",
            "second",
        ]

    def test_counts_the_funnel_by_rejection_reason(self, tmp_path: Path) -> None:
        """A base rate needs both the numerator and every way a candidate
        failed to become one."""
        store = DecisionStore(tmp_path / "decisions.sqlite")
        store.append(record("a"))
        store.append(
            record("b", accepted=False, reason=RejectionReason.NEGATIVE_NET_EDGE)
        )
        store.append(
            record("c", accepted=False, reason=RejectionReason.NEGATIVE_NET_EDGE)
        )
        store.append(
            record("d", accepted=False, reason=RejectionReason.EXCESSIVE_SKEW)
        )

        assert store.count_by_reason() == {
            "": 1,
            "negative_net_edge": 2,
            "excessive_skew": 1,
        }
