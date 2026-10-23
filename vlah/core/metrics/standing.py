"""The four standing metrics and the supporting diagnostics (v2.2 section 11).

- holdout score of the deliverable: None until the runner or closeout scores it;
  the herd never computes it, because the holdout is hidden from it (D63).
- dead-end re-walks and insight reuse: from Indexer.metrics().
- prediction error: from every candidates/<hash>/result.json, as the mean absolute
  error of predicted against actual, by the submitting agent and by approach.
Diagnostics: the best top-level score so far against cumulative machine-hours, the
spend view, 429 counts the worker loop reports, and the router's interrupt latency.
"""

import json
import statistics
import threading
from dataclasses import dataclass, field
from typing import Literal, Protocol

from vlah.core.blackboard.api import Blackboard
from vlah.core.budget.api import BudgetMeter, BudgetView
from vlah.core.indexer.api import Indexer
from vlah.core.metrics.api import StandingMetrics
from vlah.core.types.ids import ActorId

RESULT_SUFFIX = "/result.json"
MANIFEST_SUFFIX = "/manifest.yaml"


@dataclass(frozen=True, kw_only=True)
class PredictionRecord:
    submitter: str
    approach: str
    level: str
    error: float
    score: float | None


class InterruptSummary(Protocol):
    """What the router reports about interrupts (H3): its stats() satisfies this."""

    @property
    def latencies_seconds(self) -> tuple[float, ...]: ...

    @property
    def overdue_count(self) -> int: ...


class InterruptSource(Protocol):
    def stats(self) -> InterruptSummary: ...


@dataclass(frozen=True, kw_only=True)
class Diagnostics:
    """Supporting diagnostics (section 11), next to the four standing metrics."""

    best_score_curve: tuple[tuple[float, float], ...]
    spend: BudgetView
    rate_limit_events: int
    interrupts: InterruptSummary | None


def approach_of(blackboard: Blackboard, candidate: str) -> str:
    path = f"candidates/{candidate}{MANIFEST_SUFFIX}"
    if path not in blackboard.list_paths(path, None):
        return ""
    else:
        text = blackboard.read(path, None).decode("utf-8", "replace")
        for line in text.splitlines():
            if line.startswith("approach:"):
                return line.split(":", 1)[1].strip()
            else:
                continue
        return ""


def prediction_records(
    blackboard: Blackboard, top_level: str | None
) -> list[PredictionRecord]:
    records: list[PredictionRecord] = []
    for path in blackboard.list_paths("candidates/", None):
        if not path.endswith(RESULT_SUFFIX):
            continue
        else:
            data: object = json.loads(blackboard.read(path, None))
            if not isinstance(data, dict):
                continue
            else:
                candidate = path.split("/")[1]
                approach = approach_of(blackboard, candidate)
                submitter = str(data.get("submitter", ""))
                levels = data.get("levels")
                for level in levels if isinstance(levels, list) else []:
                    error = (
                        level.get("prediction_error")
                        if isinstance(level, dict)
                        else None
                    )
                    if isinstance(level, dict) and isinstance(error, (int, float)):
                        score = level.get("score")
                        records.append(
                            PredictionRecord(
                                submitter=submitter,
                                approach=approach,
                                level=str(level.get("level")),
                                error=float(error),
                                score=(
                                    float(score)
                                    if isinstance(score, (int, float))
                                    else None
                                ),
                            )
                        )
                    else:
                        continue
    return records


def mean_by(
    records: list[PredictionRecord], group: Literal["submitter", "approach"]
) -> dict[str, float]:
    groups: dict[str, list[float]] = {}
    for record in records:
        name = record.submitter if group == "submitter" else record.approach
        groups.setdefault(name, []).append(record.error)
    return {name: statistics.fmean(errors) for name, errors in sorted(groups.items())}


@dataclass(kw_only=True)
class HerdMetrics:
    blackboard: Blackboard
    indexer: Indexer
    budget: BudgetMeter
    router: InterruptSource | None
    top_level: str | None
    holdout_score: float | None = None
    rate_limit_events: int = 0
    best_score_curve: list[tuple[float, float]] = field(
        default_factory=list[tuple[float, float]]
    )
    # record_* are called from worker-loop threads; standing() from the reporter.
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_holdout(self, score: float) -> None:
        """The runner's verdict on the deliverable, once it has been scored."""
        with self.lock:
            self.holdout_score = score

    def record_rate_limits(self, agent: ActorId, count: int) -> None:
        with self.lock:
            self.rate_limit_events += count

    def record_best_score(self, score: float) -> None:
        """A new best top-level score, against the machine-hours spent so far."""
        hours = self.budget.spent().total_machine_hours
        with self.lock:
            if not self.best_score_curve or score > self.best_score_curve[-1][1]:
                self.best_score_curve.append((hours, score))
            else:
                pass

    def standing(self) -> StandingMetrics:
        indexed = self.indexer.metrics()
        records = prediction_records(self.blackboard, self.top_level)
        with self.lock:
            holdout_score = self.holdout_score
        return StandingMetrics(
            holdout_score=holdout_score,
            dead_end_rewalk_count=indexed.dead_end_rewalk_count,
            insight_reuse_counts=dict(indexed.insight_reuse_counts),
            prediction_error_by_agent=mean_by(records, "submitter"),
            prediction_error_by_approach=mean_by(records, "approach"),
        )

    def diagnostics(self) -> Diagnostics:
        with self.lock:
            curve = tuple(self.best_score_curve)
            rate_limit_events = self.rate_limit_events
        return Diagnostics(
            best_score_curve=curve,
            spend=self.budget.spent(),
            rate_limit_events=rate_limit_events,
            interrupts=self.router.stats() if self.router is not None else None,
        )
