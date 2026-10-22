"""The worker loop: complete only with a claim, own the handle, restart near the limit."""

import dataclasses
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fixtures.worker import (
    TASK,
    TRANSCRIPT_VARIABLE,
    WORKER,
    RecordingTaskQueue,
    WorkerHarness,
    assistant_line,
    make_worker_harness,
)
from vlah.adapters.memory.executor import ScriptedSession
from vlah.arms.config import PRESETS
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.tasks import Task
from vlah.core.worker.loop import NO_CLAIM_REASON, STOPPING_REASON, run_task, run_worker

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
    """The executor's own no-claim restart keeps the handle but writes a new transcript."""
    holder: list[WorkerHarness] = []
    sessions: list[ScriptedSession] = []

    def agent(session: ScriptedSession) -> Iterator[float]:
        sessions.append(session)
        transcript = Path(session.spec.env[TRANSCRIPT_VARIABLE])
        transcript.write_text(assistant_line(190_000, "Bash"))
        yield 60.0
        fresh = transcript.with_name("transcript-attempt-2.jsonl")
        fresh.write_text(assistant_line(20_000, "Read"))
        process = holder[0].executor.processes["memory-00001"]
        process.spec = process.spec.model_copy(
            update={"env": {**process.spec.env, TRANSCRIPT_VARIABLE: str(fresh)}}
        )
        yield 1_000.0
        holder[0].write_claim(WORKER, TASK, 1, "Finished in the fresh process.")

    worker = make_worker_harness(tmp_path, PRESETS["S2"], {"agent": agent}, "agent")
    holder.append(worker)
    run_task(WORKER, TASK, worker.deps, cursor=0)
    assert len(sessions) == 1, "neither killed nor restarted for context"
    assert worker.task_queue.completed == [TASK.id]


def test_a_drop_within_one_transcript_is_a_compaction_not_a_fresh_context(
    tmp_path: Path,
) -> None:
    """Copilot compacts at 80%: the wrap-up clock keeps running and the session is replaced."""
    holder: list[WorkerHarness] = []
    sessions: list[ScriptedSession] = []

    def agent(session: ScriptedSession) -> Iterator[float]:
        sessions.append(session)
        transcript = Path(session.spec.env[TRANSCRIPT_VARIABLE])
        if len(sessions) == 1:
            transcript.write_text(assistant_line(150_000, "Bash"))
            yield 60.0
            with transcript.open("a") as lines:
                lines.write(assistant_line(165_000, "herd-repo_read"))
                lines.write(assistant_line(40_000, "Bash"))
            yield 5_000.0
            holder[0].write_claim(WORKER, TASK, 1, "Done on a compacted context.")
        else:
            holder[0].write_claim(WORKER, TASK, 2, "Continued from the restart.")

    worker = make_worker_harness(tmp_path, PRESETS["S2"], {"agent": agent}, "agent")
    holder.append(worker)
    run_task(WORKER, TASK, worker.deps, cursor=0)
    assert len(sessions) == 2, "killed after the wrap-up time, then restarted"
    assert worker.clock.now_seconds() < 5_000.0
    assert "## Context restart" in sessions[1].start_pack.files["CONTEXT.md"]
    assert worker.task_queue.completed == [TASK.id]


def test_a_quick_wrap_up_still_gets_its_restart(tmp_path: Path) -> None:
    """Crossing the threshold and ending within one poll is still a D77 restart."""
    holder: list[WorkerHarness] = []
    sessions: list[ScriptedSession] = []

    def agent(session: ScriptedSession) -> Iterator[float]:
        sessions.append(session)
        transcript = Path(session.spec.env[TRANSCRIPT_VARIABLE])
        if len(sessions) == 1:
            transcript.write_text(assistant_line(60_000, "Bash"))
            yield 10.0
            with transcript.open("a") as lines:
                lines.write(assistant_line(150_000, "Edit"))
            holder[0].write_claim(WORKER, TASK, 1, "Lead: unfinished, continue me.")
            yield 5.0
        else:
            holder[0].write_claim(WORKER, TASK, 2, "Followed the lead.")

    worker = make_worker_harness(tmp_path, PRESETS["S2"], {"agent": agent}, "agent")
    holder.append(worker)
    run_task(WORKER, TASK, worker.deps, cursor=0)
    assert len(sessions) == 2, "exactly one restart"
    assert "Lead: unfinished" in sessions[1].start_pack.files["CONTEXT.md"]
    assert worker.task_queue.completed == [TASK.id]


def test_a_loop_failure_kills_the_session_before_letting_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No session outlives its lease: a second worker must not run the task beside it."""

    def long_running(session: ScriptedSession) -> Iterator[float]:
        yield 10_000.0

    worker = make_worker_harness(
        tmp_path, PRESETS["S2"], {"long_running": long_running}, "long_running"
    )

    def flaky_wait(handle: object, timeout_seconds: float | None) -> None:
        raise RuntimeError("sidecar socket closed")

    monkeypatch.setattr(worker.executor, "wait", flaky_wait)
    with pytest.raises(RuntimeError, match="sidecar"):
        run_task(WORKER, TASK, worker.deps, cursor=0)
    handle = worker.executor.processes["memory-00001"].handle
    assert not worker.executor.is_alive(handle)
    assert worker.leases.get(WORKER) is None
    assert worker.task_queue.requeued == [(TASK.id, NO_CLAIM_REASON)]
    assert worker.task_queue.completed == []


def test_a_hook_shown_interrupt_rides_the_restart_pack_once(tmp_path: Path) -> None:
    """D77 part 3: the interrupt the hook delivered, in the next pack and not again."""
    holder: list[WorkerHarness] = []
    sessions: list[ScriptedSession] = []
    sandbox = tmp_path / "sandboxes" / WORKER / TASK.id

    def agent(session: ScriptedSession) -> Iterator[float]:
        sessions.append(session)
        transcript = Path(session.spec.env[TRANSCRIPT_VARIABLE])
        if len(sessions) == 1:
            (sandbox / ".vlah").mkdir(parents=True, exist_ok=True)
            (sandbox / ".vlah" / "interrupts.jsonl").write_text(
                INTERRUPT.model_dump_json() + "\n"
            )
        else:
            pass
        if len(sessions) < 3:
            transcript.write_text(assistant_line(150_000, "Bash"))
            yield 60.0
            holder[0].write_claim(WORKER, TASK, len(sessions), "Wrapping up.")
        else:
            holder[0].write_claim(WORKER, TASK, 3, "Done.")

    worker = make_worker_harness(tmp_path, PRESETS["S2"], {"agent": agent}, "agent")
    holder.append(worker)
    run_task(WORKER, TASK, worker.deps, cursor=0)
    assert [session.start_pack.interrupt for session in sessions] == [
        None,
        INTERRUPT,
        None,
    ]
    assert worker.task_queue.completed == [TASK.id]


@dataclass(kw_only=True)
class StopWhileClaiming(RecordingTaskQueue):
    """The run stops between the loop's stop check and its claim."""

    stop: threading.Event

    def claim_task(self, agent: ActorId) -> Task | None:
        task = super().claim_task(agent)
        self.stop.set()
        return task


def test_a_task_claimed_as_the_run_stops_is_handed_back(tmp_path: Path) -> None:
    worker = make_worker_harness(tmp_path, PRESETS["S2"], {}, "none")
    queue = StopWhileClaiming(stop=worker.stop)
    queue.put(TASK)
    run_worker(WORKER, dataclasses.replace(worker.deps, task_queue=queue))
    assert queue.requeued == [(TASK.id, STOPPING_REASON)]
    assert worker.executor.started_count == 0, "no session started after the stop"


def test_no_context_restart_once_the_run_is_stopping(tmp_path: Path) -> None:
    holder: list[WorkerHarness] = []
    sessions: list[ScriptedSession] = []

    def agent(session: ScriptedSession) -> Iterator[float]:
        sessions.append(session)
        Path(session.spec.env[TRANSCRIPT_VARIABLE]).write_text(
            assistant_line(190_000, "bash")
        )
        yield 60.0
        holder[0].write_claim(WORKER, TASK, 1, "Wrapped up as the run stopped.")
        holder[0].stop.set()

    worker = make_worker_harness(tmp_path, PRESETS["S2"], {"agent": agent}, "agent")
    holder.append(worker)
    run_task(WORKER, TASK, worker.deps, cursor=0)
    assert len(sessions) == 1
    assert worker.task_queue.completed == [TASK.id]
