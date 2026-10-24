"""The status file and the stop request: how a running herd is seen and stopped from outside (#119)."""

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.fixtures.runtime import (
    MemoryAssembly,
    WaitingRunner,
    random_for_tests,
    toy_problem,
)
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.worker.loop import WorkerDeps
from vlah.runtime.compose import COMPOSE_AGENT
from vlah.runtime.herd import HerdRun, build_herd
from vlah.runtime.status import RunStatusFile, read_status
from vlah.runtime.supervisor import STOP_REQUEST_FILE_NAME

pytestmark = pytest.mark.runtime

REPOSITORY_ROOT = Path(__file__).parents[3]
STATUS_SCHEMA_PATH = REPOSITORY_ROOT / "docs" / "status-schema.json"
WORKERS = (ActorId("worker-01"), ActorId("worker-02"))
LEASE_WAIT_SECONDS = 10.0


@dataclass(kw_only=True)
class LeasingRunner:
    """Stands in for run_worker: takes one task, holds its lease, waits for the stop."""

    leased: dict[ActorId, threading.Event] = field(
        default_factory=lambda: {agent: threading.Event() for agent in WORKERS}
    )

    def __call__(self, agent: ActorId, deps: WorkerDeps) -> None:
        task = deps.task_queue.claim_task(agent)
        if task is not None:
            deps.leases.acquire(agent, task, f"handle-{agent}")
            self.leased[agent].set()
        else:
            pass
        deps.stop.wait(None)


def prepared_run(
    tmp_path: Path, clock: FakeClock, runner: LeasingRunner | WaitingRunner
) -> HerdRun:
    return build_herd(
        toy_problem(tmp_path),
        "S2",
        MemoryAssembly(clock=clock, root=tmp_path),
        "r-1",
        root=tmp_path,
        worker_count=len(WORKERS),
        duration_seconds=3600.0,
        clock=clock,
        random=random_for_tests(),
        runner=runner,
    ).prepare()


def test_the_status_file_shows_every_workers_task(tmp_path: Path) -> None:
    """Leases and the queue lived only in the run's memory, so nothing outside the
    process could tell which worker held which task."""
    clock = FakeClock()
    runner = LeasingRunner()
    run = prepared_run(tmp_path, clock, runner)
    run.tick(clock.now_seconds())
    for agent, leased in runner.leased.items():
        assert leased.wait(LEASE_WAIT_SECONDS), f"{agent} took no task"
    clock.advance(30.0)
    run.tick(clock.now_seconds())
    status = read_status(run.status_path)
    leases = {lease.agent: lease for lease in run.components.leases.active()}
    workers = {worker.agent: worker for worker in status.workers}
    assert status.phase == "running" and status.written_ts == 30.0
    for agent in WORKERS:
        task = workers[agent].task
        assert task is not None and task.id == leases[agent].task_id
        assert task.approach == leases[agent].approach and task.kind == "advance"
        assert workers[agent].session_count == 1
        assert workers[agent].lease_age_seconds == 30.0
    assert workers[COMPOSE_AGENT].task is None and workers[COMPOSE_AGENT].is_compose
    run.close()
    finished = read_status(run.status_path)
    assert finished.phase == "finished" and finished.deliverable_baseline == 0


def test_a_stop_request_left_by_an_earlier_process_is_dropped(tmp_path: Path) -> None:
    """A request no supervisor consumed (its process died) must not stop the next
    process that opens the same run."""
    (tmp_path / "r-1").mkdir()
    (tmp_path / "r-1" / STOP_REQUEST_FILE_NAME).write_text("{}", encoding="utf-8")
    clock = FakeClock()
    run = prepared_run(tmp_path, clock, WaitingRunner())
    run.tick(clock.now_seconds())
    assert not run.supervisor.is_stopping
    (run.context.run_root / STOP_REQUEST_FILE_NAME).write_text("{}", encoding="utf-8")
    run.tick(clock.now_seconds())
    assert (
        run.supervisor.is_stopping and run.supervisor.stop_state.reason == "vlah stop"
    )
    assert not (run.context.run_root / STOP_REQUEST_FILE_NAME).exists()
    run.close()


def test_the_documented_status_schema_matches_the_code() -> None:
    """The observatory UI (#111) validates against docs/status-schema.json."""
    documented: object = json.loads(STATUS_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert documented == RunStatusFile.model_json_schema(), (
        "regenerate docs/status-schema.json from RunStatusFile.model_json_schema() "
        "and bump schema_version if a field changed"
    )
