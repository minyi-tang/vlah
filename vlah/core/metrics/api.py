"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

The four standing metrics (v2.2 section 11). Implemented by W1-10 (#20).
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, kw_only=True)
class StandingMetrics:
    """holdout_score stays None until the deliverable has been scored."""

    holdout_score: float | None
    dead_end_rewalk_count: int
    insight_reuse_counts: dict[str, int]
    prediction_error_by_agent: dict[str, float]
    prediction_error_by_approach: dict[str, float]


class Metrics(Protocol):
    def standing(self) -> StandingMetrics: ...
