"""The supervisor: overdue interrupts, crashed workers, the stop paths and closeout (D42, D60, D79)."""

import time
from pathlib import Path

import pytest

from tests.fixtures.runtime import (
    FakeIntegrator,
    MemoryAssembly,
    ScriptedStopRules,
    SwitchedRouter,
    WaitingRunner,
    random_for_tests,
    toy_problem,
)
from vlah.arms.config import ArmName
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.runtime.compose import COMPOSE_AGENT
from vlah.runtime.herd import HerdRun, build_herd

pytestmark = pytest.mark.runtime

DURATION_SECONDS = 3600.0


def prepared_run(tmp_path: Path, runner: WaitingRunner, arm: ArmName = "S2") -> HerdRun:
    clock = FakeClock()
    herd = build_herd(
        toy_problem(tmp_path),
        arm,
        MemoryAssembly(clock=clock, root=tmp_path),
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
    """The worker loop calls Executor.interrupt itself, so it keeps owning the handle."""
    run = prepared_run(tmp_path, WaitingRunner())
    router = run.components.router
    assert isinstance(router, SwitchedRouter)
    interrupt = Interrupt(
        id="i-1",
        agent=ActorId("worker-01"),
        kind="hard",
        rule=2,
        reason="r",
        written_ts=0.0,
    )
    router.overdue_interrupts.append(interrupt)
    try:
        run.supervisor.tick(0.0)
        assert run.supervisor.inbox.take(ActorId("worker-01")) == interrupt
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
    stop_rules = run.components.stop_rules
    assert isinstance(stop_rules, ScriptedStopRules)
    if trigger == "stop rule":
        stop_rules.stop_at_seconds = 10.0
        supervisor.tick(9.0)
        assert not supervisor.is_stopping
        supervisor.tick(10.0)
    elif trigger == "time limit":
        supervisor.tick(DURATION_SECONDS)
    else:
        supervisor.request_stop("first")
        supervisor.request_stop("second")
    assert supervisor.is_stopping
    supervisor.finish()
    supervisor.finish()
    integrator = run.components.integrator
    assert isinstance(integrator, FakeIntegrator)
    assert integrator.closeout_count == 1
    assert supervisor.stop_state.reason is not None


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
    run = prepared_run(tmp_path, WaitingRunner())
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
