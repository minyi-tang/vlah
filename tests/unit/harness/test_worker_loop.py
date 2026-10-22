"""The worker loop: complete only with a claim, own the handle, restart near the limit."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.worker import (
    TASK,
    TRANSCRIPT_VARIABLE,
    WORKER,
    WorkerHarness,
    assistant_line,
    make_worker_harness,
)
from vlah.adapters.memory.executor import ScriptedSession
from vlah.arms.config import PRESETS
from vlah.core.types.interrupts import Interrupt
from vlah.core.worker.loop import NO_CLAIM_REASON, run_task, run_worker

pytestmark = pytest.mark.harness

INTERRUPT = Interrupt(
    id="i-1", agent=WORKER, kind="hard", rule=2, reason="refuted", written_ts=0.0
)


def test_a_session_without_a_claim_is_not_marked_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def silent(session: ScriptedSession) -> Iterator[float]:
        yield 10.0

    worker = make_worker_harness(tmp_path, PRESETS["S2"], {"silent": silent}, "silent")
    held_at_requeue: list[bool] = []
    requeue = worker.task_queue.requeue

    def requeue_after_release(task_id: str, reason: str) -> None:
        held_at_requeue.append(worker.leases.get(WORKER) is not None)
        requeue(task_id, reason)

    monkeypatch.setattr(worker.task_queue, "requeue", requeue_after_release)
    run_task(WORKER, TASK, worker.deps, cursor=0)
    assert worker.task_queue.completed == []
    assert worker.task_queue.requeued == [(TASK.id, NO_CLAIM_REASON)]
    assert held_at_requeue == [False], "the queue refuses a task that is still held"
    assert worker.leases.get(WORKER) is None


def test_a_session_with_a_claim_completes_its_task(tmp_path: Path) -> None:
    holder: list[WorkerHarness] = []

    def diligent(session: ScriptedSession) -> Iterator[float]:
        yield 10.0
        holder[0].write_claim(WORKER, TASK, 1, "Found it.")

    worker = make_worker_harness(
        tmp_path, PRESETS["S2"], {"diligent": diligent}, "diligent"
    )
    holder.append(worker)
    run_task(WORKER, TASK, worker.deps, cursor=0)
    assert worker.task_queue.completed == [TASK.id]
    assert worker.registry.registered == [
        (WORKER, tmp_path / "sandboxes" / WORKER / TASK.id, TASK.id)
    ], "eval_request paths resolve in this task's sandbox"
    assert worker.leases.acquired_handles == ["memory-00001"]


def test_an_overdue_interrupt_restarts_the_session_under_the_loop(
    tmp_path: Path,
) -> None:
    holder: list[WorkerHarness] = []
    sessions: list[ScriptedSession] = []

    def agent(session: ScriptedSession) -> Iterator[float]:
        sessions.append(session)
        if session.start_pack.interrupt is None:
            yield 600.0
        else:
            holder[0].write_claim(WORKER, TASK, 1, "Rebased and done.")

    worker = make_worker_harness(tmp_path, PRESETS["S2"], {"agent": agent}, "agent")
    holder.append(worker)
    worker.inbox.post(INTERRUPT)
    run_task(WORKER, TASK, worker.deps, cursor=0)
    assert [session.start_pack.interrupt for session in sessions] == [None, INTERRUPT]
    assert worker.leases.acquired_handles == ["memory-00001", "memory-00002"]
    assert worker.task_queue.completed == [TASK.id]


def test_an_agent_that_ignores_the_context_limit_is_stopped_and_restarted(
    tmp_path: Path,
) -> None:
    holder: list[WorkerHarness] = []
    sessions: list[ScriptedSession] = []

    def agent(session: ScriptedSession) -> Iterator[float]:
        sessions.append(session)
        if len(sessions) == 1:
            Path(session.spec.env[TRANSCRIPT_VARIABLE]).write_text(
                assistant_line(190_000, "Bash")
            )
            yield 10_000.0
        else:
            holder[0].write_claim(WORKER, TASK, 1, "Picked up from the restart.")

    worker = make_worker_harness(tmp_path, PRESETS["S2"], {"agent": agent}, "agent")
    holder.append(worker)
    run_task(WORKER, TASK, worker.deps, cursor=0)
    assert len(sessions) == 2
    assert "## Context restart" in sessions[1].start_pack.files["CONTEXT.md"]
    assert worker.clock.now_seconds() < 10_000.0, "stopped after the wrap-up time"
    assert worker.task_queue.completed == [TASK.id]


def test_run_worker_takes_tasks_until_stopped(tmp_path: Path) -> None:
    holder: list[WorkerHarness] = []

    def finishing(session: ScriptedSession) -> Iterator[float]:
        yield 5.0
        holder[0].write_claim(WORKER, session.task, 1, "Done.")
        holder[0].stop.set()

    worker = make_worker_harness(
        tmp_path, PRESETS["S2"], {"finishing": finishing}, "finishing"
    )
    holder.append(worker)
    worker.task_queue.put(TASK)
    run_worker(WORKER, worker.deps)
    assert worker.task_queue.completed == [TASK.id]


def test_a_replaced_process_starts_the_wrap_up_clock_over(tmp_path: Path) -> None:
    """The executor's own no-claim restart keeps the handle but starts a fresh context."""
    holder: list[WorkerHarness] = []
    sessions: list[ScriptedSession] = []

    def agent(session: ScriptedSession) -> Iterator[float]:
        sessions.append(session)
        transcript = Path(session.spec.env[TRANSCRIPT_VARIABLE])
        transcript.write_text(assistant_line(190_000, "Bash"))
        yield 60.0
        transcript.write_text(assistant_line(20_000, "Read"))
        yield 1_000.0
        holder[0].write_claim(WORKER, TASK, 1, "Finished in the fresh process.")

    worker = make_worker_harness(tmp_path, PRESETS["S2"], {"agent": agent}, "agent")
    holder.append(worker)
    run_task(WORKER, TASK, worker.deps, cursor=0)
    assert len(sessions) == 1, "neither killed nor restarted for context"
    assert worker.task_queue.completed == [TASK.id]
