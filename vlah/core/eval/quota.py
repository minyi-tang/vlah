"""Judge quota, counted by the eval client and nobody else (v2.2 D40, D74; TB section 11.8.1).

Three budgets:
- each level's daily runs (budget.per_day);
- each worker's daily runs at the cheaper levels (t1_per_worker_per_day);
- the Integrator's own remeasure quota, a total and a per-hour cap.
"""

from dataclasses import dataclass, field
from typing import Literal

from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.sources import SourceDecl

DAY_SECONDS = 86400.0
HOUR_SECONDS = 3600.0

RequestKind = Literal["worker", "remeasure"]


@dataclass(kw_only=True)
class QuotaTracker:
    clock: Clock
    params: Params
    top_level_id: str
    runs_by_level: dict[str, list[float]] = field(
        default_factory=dict[str, list[float]]
    )
    cheap_runs_by_worker: dict[ActorId, list[float]] = field(
        default_factory=dict[ActorId, list[float]]
    )
    integrator_runs: list[float] = field(default_factory=list[float])

    def count_since(self, run_times: list[float], window_seconds: float) -> int:
        now_seconds = self.clock.now_seconds()
        return sum(
            1 for run_time in run_times if run_time > now_seconds - window_seconds
        )

    def allows(self, kind: RequestKind, caller: ActorId, level: SourceDecl) -> bool:
        if kind == "remeasure":
            return (
                len(self.integrator_runs) < self.params.quota_integrator_total
                and self.count_since(self.integrator_runs, HOUR_SECONDS)
                < self.params.quota_integrator_per_hour
            )
        elif self.count_since(self.runs_by_level.get(level.id, []), DAY_SECONDS) >= (
            level.budget.per_day
        ):
            return False
        elif level.id != self.top_level_id:
            return (
                self.count_since(self.cheap_runs_by_worker.get(caller, []), DAY_SECONDS)
                < self.params.t1_per_worker_per_day
            )
        else:
            return True

    def record(self, kind: RequestKind, caller: ActorId, level: SourceDecl) -> None:
        now_seconds = self.clock.now_seconds()
        if kind == "remeasure":
            self.integrator_runs.append(now_seconds)
        else:
            self.runs_by_level.setdefault(level.id, []).append(now_seconds)
            if level.id != self.top_level_id:
                self.cheap_runs_by_worker.setdefault(caller, []).append(now_seconds)
            else:
                pass
