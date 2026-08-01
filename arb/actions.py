"""Actions - everything the reducer asks the shell to do.

The reducer returns these; it never performs them. `EmitDecisionRecord` is the
important one: that stream *is* the decision log the verdict is computed from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from arb.decisions import DecisionRecord

__all__ = ["Action", "EmitDecisionRecord"]


@dataclass(frozen=True, slots=True)
class EmitDecisionRecord:
    """Persist one evaluation, accepted or rejected."""

    record: DecisionRecord


Action: TypeAlias = EmitDecisionRecord
