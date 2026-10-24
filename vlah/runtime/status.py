"""The run's status file, the one outside view of a running herd (#119).

Leases, the queue and the supervisor's slots live only in the run's process. The
runtime writes <run root>/status.json after every supervisor tick, when closing
starts and once the closeout has run. It writes a temporary file and renames it
over the old one, so a reader always sees one whole status.
docs/status-file.md describes the fields, and docs/status-schema.json is generated
from RunStatusFile. Any change to the fields bumps schema_version.
"""

import os
import statistics
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from vlah.arms.config import ArmName
from vlah.core.blackboard.api import MergedCommit
from vlah.core.queue.api import HandleLiveness, Leases
from vlah.core.queue.queue import KeyValueTaskQueue
from vlah.core.types.base import FrozenModel
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Lease, Task, TaskKind
from vlah.runtime.assembly import QueuedTaskCounts, RunComponents, RunContext
from vlah.runtime.supervisor import Supervisor

STATUS_FILE_NAME = "status.json"
STATUS_SCHEMA_VERSION = 1
RECENT_EVENT_COUNT = 20

RunPhase = Literal["running", "closing", "finished"]


class TaskStatus(FrozenModel):
    id: str
    kind: TaskKind
    approach: str
    goal: str
    priority: Literal[0, 1, 2]
    refs: tuple[str, ...]


class WorkerStatus(FrozenModel):
    """One worker slot; task is None while the slot holds no lease."""

    agent: str
    is_compose: bool
    task: TaskStatus | None
    lease_age_seconds: float | None
    last_heartbeat_ts: float | None
    files_touched: tuple[str, ...]
    session_count: int
    crash_count: int
    last_write_ts: float | None


class QueueStatus(FrozenModel):
    arm_waiting: int
    compose_waiting: int


class SourceStatus(FrozenModel):
    """One judge: runs_used counts the runs it made, never cache hits."""

    source: str
    runs_used: int
    run_cap: int | None
    per_day_cap: int
    machine_hours_used: float


class JudgeStatus(FrozenModel):
    runs_used: int
    machine_hours_used: float
    machine_hours_limit: float
    sources: tuple[SourceStatus, ...]


class BaselineStatus(FrozenModel):
    """The Integrator's current baseline; score is the mean of its per-seed scores."""

    k: int
    score: float | None
    candidate_count: int
    guard_pending: bool


class RunStatusFile(FrozenModel):
    schema_version: Literal[1]
    run_id: str
    arm: ArmName
    phase: RunPhase
    written_ts: float
    started_ts: float
    deadline_ts: float
    poll_seconds: float
    stop_reason: str | None
    closeout_error: str | None
    deliverable_baseline: int | None
    head_sequence: int
    workers: tuple[WorkerStatus, ...]
    queues: QueueStatus
    judge: JudgeStatus
    baseline: BaselineStatus
    recent_events: tuple[Event, ...]


@dataclass(kw_only=True)
class SessionLedger:
    """What a lease does not keep: the whole task, and how many sessions an agent started."""

    tasks: dict[ActorId, Task] = field(default_factory=dict[ActorId, Task])
    session_counts: dict[ActorId, int] = field(default_factory=dict[ActorId, int])
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, agent: ActorId, task: Task) -> None:
        with self.lock:
            self.tasks[agent] = task
            self.session_counts[agent] = self.session_counts.get(agent, 0) + 1


class LedgerLeases:
    """The workers' leases (queue.api.Leases): the worker loop acquires a lease for
    every session it starts, so each acquire is recorded in the ledger."""

    def __init__(self, *, leases: Leases, ledger: SessionLedger) -> None:
        self.leases = leases
        self.ledger = ledger

    def acquire(self, agent: ActorId, task: Task, handle_id: str) -> Lease:
        lease = self.leases.acquire(agent, task, handle_id)
        self.ledger.record(agent, task)
        return lease

    def beat(
        self, agent: ActorId, files_touched: tuple[str, ...], timestamp_seconds: float
    ) -> None:
        self.leases.beat(agent, files_touched, timestamp_seconds)

    def add_targets(
        self, agent: ActorId, claim_ids: tuple[str, ...], depends_on: tuple[str, ...]
    ) -> None:
        self.leases.add_targets(agent, claim_ids, depends_on)

    def release(self, agent: ActorId) -> None:
        self.leases.release(agent)

    def get(self, agent: ActorId) -> Lease | None:
        return self.leases.get(agent)

    def active(self) -> tuple[Lease, ...]:
        return self.leases.active()

    def reclaim_stale(
        self, now_seconds: float, liveness: HandleLiveness
    ) -> tuple[str, ...]:
        return self.leases.reclaim_stale(now_seconds, liveness)


@dataclass(kw_only=True)
class RunActivity:
    """A merge listener: the last events merged, and when each actor last wrote."""

    clock: Clock
    recent_events: deque[Event] = field(
        default_factory=lambda: deque[Event](maxlen=RECENT_EVENT_COUNT)
    )
    last_write_ts: dict[str, float] = field(default_factory=dict[str, float])
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, merged_commit: MergedCommit) -> None:
        with self.lock:
            self.recent_events.extend(merged_commit.events)
            self.last_write_ts[merged_commit.actor] = self.clock.now_seconds()


@dataclass(frozen=True, kw_only=True)
class KeyValueQueueCounts:
    """assembly.CountQueuedTasks over the key-value queues an assembly built."""

    task_queue: KeyValueTaskQueue
    compose_queue: KeyValueTaskQueue

    def __call__(self) -> QueuedTaskCounts:
        return QueuedTaskCounts(
            arm=len(self.task_queue.queued_tasks()),
            compose=len(self.compose_queue.queued_tasks()),
        )


def worker_statuses(
    supervisor: Supervisor,
    leases: tuple[Lease, ...],
    ledger: SessionLedger,
    activity: RunActivity,
    now_seconds: float,
) -> tuple[WorkerStatus, ...]:
    lease_by_agent = {lease.agent: lease for lease in leases}
    with ledger.lock:
        tasks = dict(ledger.tasks)
        session_counts = dict(ledger.session_counts)
    with activity.lock:
        last_write_ts = dict(activity.last_write_ts)
    statuses: list[WorkerStatus] = []
    for slot in supervisor.slots:
        lease = lease_by_agent.get(slot.agent)
        task = tasks.get(slot.agent)
        # The ledger keeps an agent's last task; it is current only while leased.
        current_task = (
            task
            if lease is not None and task is not None and task.id == lease.task_id
            else None
        )
        statuses.append(
            WorkerStatus(
                agent=slot.agent,
                is_compose=slot.is_compose,
                task=(
                    TaskStatus(
                        id=current_task.id,
                        kind=current_task.kind,
                        approach=current_task.approach,
                        goal=current_task.goal,
                        priority=current_task.priority,
                        refs=current_task.refs,
                    )
                    if current_task is not None
                    else None
                ),
                lease_age_seconds=(
                    now_seconds - lease.acquired_ts if lease is not None else None
                ),
                last_heartbeat_ts=lease.heartbeat_ts if lease is not None else None,
                files_touched=lease.files_touched if lease is not None else (),
                session_count=session_counts.get(slot.agent, 0),
                crash_count=slot.crash_count,
                last_write_ts=last_write_ts.get(slot.agent),
            )
        )
    return tuple(statuses)


def build_status(
    *,
    context: RunContext,
    components: RunComponents,
    supervisor: Supervisor,
    ledger: SessionLedger,
    activity: RunActivity,
    judge_runs_by_source: dict[str, int],
    poll_seconds: float,
) -> RunStatusFile:
    now_seconds = context.clock.now_seconds()
    stop_state = supervisor.stop_state
    is_closed = stop_state.deliverable is not None or stop_state.error is not None
    phase: RunPhase = (
        "finished" if is_closed else "closing" if supervisor.is_stopping else "running"
    )
    baseline = components.integrator.current()
    spent = components.budget.spent()
    limits = context.problem.limits
    queued = components.count_queued_tasks()
    with activity.lock:
        recent_events = tuple(activity.recent_events)
    return RunStatusFile(
        schema_version=STATUS_SCHEMA_VERSION,
        run_id=context.run_id,
        arm=context.arm.arm,
        phase=phase,
        written_ts=now_seconds,
        started_ts=supervisor.started_seconds,
        deadline_ts=supervisor.deadline_seconds,
        poll_seconds=poll_seconds,
        stop_reason=stop_state.reason,
        closeout_error=stop_state.error,
        deliverable_baseline=(
            stop_state.deliverable.k if stop_state.deliverable is not None else None
        ),
        head_sequence=components.blackboard.head_sequence(),
        workers=worker_statuses(
            supervisor, components.leases.active(), ledger, activity, now_seconds
        ),
        queues=QueueStatus(arm_waiting=queued.arm, compose_waiting=queued.compose),
        judge=JudgeStatus(
            runs_used=sum(judge_runs_by_source.values()),
            machine_hours_used=spent.total_machine_hours,
            machine_hours_limit=limits.eval_machine_hours,
            sources=tuple(
                SourceStatus(
                    source=source.id,
                    runs_used=judge_runs_by_source.get(source.id, 0),
                    run_cap=limits.runs.get(source.id),
                    per_day_cap=source.budget.per_day,
                    machine_hours_used=spent.machine_hours_by_source.get(
                        source.id, 0.0
                    ),
                )
                for source in context.problem.sources
            ),
        ),
        baseline=BaselineStatus(
            k=baseline.k,
            score=statistics.fmean(baseline.per_seed) if baseline.per_seed else None,
            candidate_count=len(baseline.candidates),
            guard_pending=baseline.guard_pending,
        ),
        recent_events=recent_events,
    )


def write_status(path: Path, status: RunStatusFile) -> None:
    """Write beside the file, then rename over it: a reader never sees half a status."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.partial")
    temporary_path.write_text(status.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary_path, path)


def read_status(path: Path) -> RunStatusFile:
    return RunStatusFile.model_validate_json(path.read_text(encoding="utf-8"))
