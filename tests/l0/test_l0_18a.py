"""L0-18a (v2.2 §14.1). Owner: W1-08 (#18).

Context usage goes over 70% → write a claim and start a new session with the start pack (D77).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.worker import (
    TASK,
    WORKER,
    WorkerHarness,
    assistant_line,
    make_worker_harness,
    transcript_of,
)
from vlah.adapters.memory.executor import ScriptedSession
from vlah.arms.config import PRESETS
from vlah.core.worker.loop import run_task

pytestmark = [pytest.mark.l0, pytest.mark.harness]


def test_l0_18a_context_restart(tmp_path: Path) -> None:
    """Context usage goes over 70% → write a claim and start a new session with the start pack (D77)."""
    holder: list[WorkerHarness] = []
    sessions: list[ScriptedSession] = []

    def agent(session: ScriptedSession) -> Iterator[float]:
        """First session: fill the context, then wrap up as the hook asks. Second: finish."""
        sessions.append(session)
        worker = holder[0]
        transcript = transcript_of(session)
        if len(sessions) == 1:
            transcript.write_text(assistant_line(100_000, "Bash"))
            yield 60.0
            with transcript.open("a") as lines:
                lines.write(assistant_line(150_000, "Edit"))
            yield 60.0
            worker.write_claim(WORKER, TASK, 1, "Lead: the cache misses. Unfinished.")
        else:
            worker.write_claim(WORKER, TASK, 2, "Followed the lead; it holds.")

    worker = make_worker_harness(tmp_path, PRESETS["S2"], {"agent": agent}, "agent")
    holder.append(worker)
    run_task(WORKER, TASK, worker.deps, cursor=0)

    assert len(sessions) == 2, "a second session replaced the one near its limit"
    restart_context = sessions[1].start_pack.files["CONTEXT.md"]
    assert restart_context.startswith(
        sessions[0].start_pack.files["CONTEXT.md"].rstrip()
    )
    assert "Lead: the cache misses." in restart_context
    assert "Bash" in restart_context and "Edit" in restart_context
    assert worker.task_queue.completed == [TASK.id]
    assert worker.leases.get(WORKER) is None
