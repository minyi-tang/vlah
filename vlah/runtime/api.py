"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

The herd runtime: the composition root that runs one arm (v2.2 D58, D62, D67).
Implemented by W1-11 (#21).
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vlah.arms.config import ArmName
from vlah.core.metrics.api import StandingMetrics
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.events import Event


@dataclass(frozen=True, kw_only=True)
class RunStatus:
    run_id: str
    arm: ArmName
    is_running: bool
    head_sequence: int
    current_baseline: int
    judge_runs_used: int
    worker_count: int


@dataclass(frozen=True, kw_only=True)
class Report:
    """The deliverable is the last baseline that is not guard_pending (D62, D74)."""

    run_id: str
    arm: ArmName
    deliverable: BaselineRecord
    baselines: tuple[BaselineRecord, ...]
    metrics: StandingMetrics


class Run(Protocol):
    def events(self) -> Iterator[Event]: ...

    def status(self) -> RunStatus: ...

    def stop(self) -> None:
        """Stop cleanly: close out the Integrator and keep everything learned."""
        ...

    def report(self) -> Report: ...


class Herd(Protocol):
    def start(self) -> Run: ...


@dataclass(frozen=True, kw_only=True)
class ArmRunRequest:
    """What tb run --arm herd passes to the herd (Herd Testbed section 11.8)."""

    arm: ArmName
    problem_root: Path
    slow_eval_url: str
    run_id: str
    worker_count: int
    duration_hours: float


class RunArm(Protocol):
    def __call__(self, request: ArmRunRequest) -> Report: ...


class RunDemo(Protocol):
    def __call__(self, seed: int) -> Report:
        """The seeded in-memory demo; the same seed gives the same deliverable."""
        ...
