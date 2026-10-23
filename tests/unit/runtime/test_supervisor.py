"""The supervisor: overdue interrupts, crashed workers, the stop paths and closeout (D42, D60, D79)."""

import time
from pathlib import Path

import pytest

from tests.fixtures.herd import TOY_SOURCE
from tests.fixtures.runtime import (
    FakeIntegrator,
    MemoryAssembly,
    SwitchedRouter,
    WaitingRunner,
    random_for_tests,
    result_ready_event,
    toy_candidate,
    toy_problem,
)
from vlah.arms.config import ArmName
from vlah.core.eval.api import EvalRequest
from vlah.core.integrator.service import HerdIntegrator
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import BaselineAdvanced, Stopped
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task
from vlah.runtime.compose import COMPOSE_AGENT
from vlah.runtime.herd import HerdRun, baselines_on, build_herd, events_on

pytestmark = pytest.mark.runtime

DURATION_SECONDS = 3600.0
WORKER = ActorId("worker-01")


def prepared_run(
    tmp_path: Path,
    runner: WaitingRunner,
    arm: ArmName = "S2",
    use_real_router: bool = True,
) -> HerdRun:
    clock = FakeClock()
    herd = build_herd(
        toy_problem(tmp_path),
        arm,
        MemoryAssembly(clock=clock, root=tmp_path, use_real_router=use_real_router),
        "r-1",
        root=tmp_path,
        worker_count=2,
        duration_seconds=DURATION_SECONDS,
        clock=clock,
        random=random_for_tests(),
        runner=runner,
    )
    return herd.prepare()


def test_overdue_interrupts_reach_the_workers_inbox(tmp_path: Path) -> None:
    """The real router's fallback: the worker loop calls Executor.interrupt itself."""
    run = prepared_run(tmp_path, WaitingRunner())
    worker = ActorId("worker-01")
    run.components.leases.acquire(
        worker,
        Task(id="t-1", approach="a-01", kind="advance", goal="g", created_by="queue"),
        "handle-1",
    )
    run.components.blackboard.write(
        ActorId("eval_service"),
        {"events/eval_service/00001.json": result_ready_event(worker).encode()},
        "result",
    )
    assert run.components.router.check(worker) is not None
    try:
        run.supervisor.tick(Params().interrupt_ack_timeout_seconds - 1)
        assert run.supervisor.inbox.take(worker) is None
        run.supervisor.tick(Params().interrupt_ack_timeout_seconds)
        delivered = run.supervisor.inbox.take(worker)
        assert delivered is not None and delivered.rule == 1
    finally:
        run.supervisor.finish()


def test_a_crashed_worker_is_started_again(tmp_path: Path) -> None:
    runner = WaitingRunner(crash_first_start=True)
    run = prepared_run(tmp_path, runner)
    run.supervisor.tick(0.0)
    for slot in run.supervisor.slots:
        assert slot.thread is not None
        slot.thread.join(0.2)
    crashed = [slot for slot in run.supervisor.slots if slot.crash_count == 1]
    assert len(crashed) == len(run.supervisor.slots), "every first start crashed"
    run.supervisor.tick(1.0)
    run.supervisor.finish()
    for agent in ("worker-01", "worker-02", COMPOSE_AGENT):
        assert runner.started.count(ActorId(agent)) == 2


@pytest.mark.parametrize("trigger", ["stop rule", "time limit", "stop twice"])
def test_every_stop_path_closes_out_exactly_once(tmp_path: Path, trigger: str) -> None:
    run = prepared_run(tmp_path, WaitingRunner())
    supervisor = run.supervisor
    if trigger == "stop rule":
        # The real rules: spending the whole limit stops the run (D52).
        limit = run.context.problem.limits.eval_machine_hours
        run.components.budget.record("worker", "a-01", "toy_slow", limit / 2)
        supervisor.tick(9.0)
        assert not supervisor.is_stopping
        run.components.budget.record("worker", "a-01", "toy_slow", limit / 2)
        escalation = supervisor.tick(10.0)
        assert [event.type for event in escalation] == ["escalation"]
        assert not supervisor.is_stopping, "the 80% escalation goes first"
        stopped = supervisor.tick(11.0)
        assert [event.type for event in stopped] == ["stopped"]
        assert supervisor.is_stopping, "a stop rule"
    elif trigger == "time limit":
        supervisor.tick(DURATION_SECONDS)
    else:
        supervisor.request_stop("first", "human")
        supervisor.request_stop("second", "human")
    assert supervisor.is_stopping
    supervisor.finish()
    supervisor.finish()
    integrator = run.components.integrator
    assert isinstance(integrator, FakeIntegrator)
    assert integrator.closeout_count == 1
    assert supervisor.stop_state.reason is not None
    stopped = [
        event.data
        for event in events_on(run.components.blackboard)
        if isinstance(event.data, Stopped)
    ]
    expected = {"stop rule": "budget", "time limit": "time", "stop twice": "human"}
    assert [event.reason for event in stopped] == [expected[trigger]]


def test_the_real_integrator_merges_a_verified_candidate_inside_the_run(
    tmp_path: Path,
) -> None:
    """The whole plain-code loop, in memory: a worker's candidate goes through the
    real eval client, the fan-out hands its verified claim to the real Integrator,
    and the supervisor's ticks merge it into baseline@1 (D74)."""
    clock = FakeClock()
    herd = build_herd(
        toy_problem(tmp_path, worker_count=1),
        "S2",
        MemoryAssembly(
            clock=clock, root=tmp_path, use_real_eval=True, use_real_integrator=True
        ),
        "r-1",
        root=tmp_path,
        worker_count=1,
        duration_seconds=DURATION_SECONDS,
        clock=clock,
        random=random_for_tests(),
        runner=WaitingRunner(),
    )
    run = herd.prepare()
    assert isinstance(run.components.integrator, HerdIntegrator)
    response = run.components.eval_service.request(
        EvalRequest(
            candidate_directory=toy_candidate(tmp_path / "worker", WORKER, 2.0),
            predictions={TOY_SOURCE.id: 2.0},
            rationale="worker-01 raises every seed by two",
        ),
        WORKER,
    )
    assert response.status == "queued"
    for _ in range(3):
        run.supervisor.tick(clock.now_seconds())
        clock.advance(TOY_SOURCE.latency_seconds + 1.0)
    assert run.status().current_baseline == 1
    (advanced,) = [
        event.data
        for event in events_on(run.components.blackboard)
        if isinstance(event.data, BaselineAdvanced)
    ]
    assert advanced.baseline == 1 and advanced.paired.mean == 2.0
    (record,) = [
        record for record in baselines_on(run.components.blackboard) if record.k == 1
    ]
    assert record.added == advanced.candidate
    run.stop()
    report = run.report()
    assert report.deliverable.k == 1
    assert report.deliverable.candidates == (advanced.candidate,)
    stopped = [
        event.data
        for event in events_on(run.components.blackboard)
        if isinstance(event.data, Stopped)
    ]
    assert [event.reason for event in stopped] == ["human"]


def test_a_started_run_stops_and_reports_the_closeout(tmp_path: Path) -> None:
    clock = FakeClock()
    runner = WaitingRunner()
    herd = build_herd(
        toy_problem(tmp_path),
        "S2",
        MemoryAssembly(clock=clock, root=tmp_path),
        "r-1",
        root=tmp_path,
        worker_count=2,
        duration_seconds=DURATION_SECONDS,
        clock=clock,
        random=random_for_tests(),
        runner=runner,
        poll_seconds=0.01,
    )
    run = herd.start()
    assert run.status().is_running
    for _ in range(500):
        if len(set(runner.started)) == 3:
            break
        else:
            time.sleep(0.01)
    run.stop()
    status = run.status()
    assert not status.is_running and status.worker_count == 2
    report = run.report()
    assert report.deliverable.k == 0 and report.arm == "S2"
    assert sorted(set(runner.started)) == [COMPOSE_AGENT, "worker-01", "worker-02"]


def test_a_failing_step_cannot_keep_the_run_past_its_limit(tmp_path: Path) -> None:
    run = prepared_run(tmp_path, WaitingRunner(), use_real_router=False)
    router = run.components.router
    assert isinstance(router, SwitchedRouter)
    router.is_failing = True
    run.supervisor.tick(1.0)
    assert not run.supervisor.is_stopping
    run.supervisor.tick(DURATION_SECONDS)
    assert run.supervisor.is_stopping, "the deadline is checked before every step"
    run.supervisor.finish()


def test_a_failed_closeout_is_reported_not_hidden(tmp_path: Path) -> None:
    run = prepared_run(tmp_path, WaitingRunner())
    integrator = run.components.integrator
    assert isinstance(integrator, FakeIntegrator)
    integrator.closeout_fails = True
    with pytest.raises(RuntimeError, match="shared merger"):
        run.stop()
    with pytest.raises(RuntimeError, match="closeout failed"):
        run.report()


def test_stopping_a_run_that_never_started_closes_it_out(tmp_path: Path) -> None:
    run = prepared_run(tmp_path, WaitingRunner())
    run.stop()
    integrator = run.components.integrator
    assert isinstance(integrator, FakeIntegrator) and integrator.closeout_count == 1
    assert not run.status().is_running
