"""The local executor: heartbeats on its own clock, restarts, interrupts, stubs (D32, D60, D77)."""

import json
import os
import time
from pathlib import Path

import pytest

from tests.fixtures.executor import (
    QUICK_PROGRAM,
    SLEEP_PROGRAM,
    START_PACK,
    TASK,
    WORKER,
    RealTimer,
    SteppedTimer,
    git,
    running_executor,
    wait_for_file,
)
from vlah.adapters.executor.local.traces import RESTART_HINT, STUB_TAG
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import FakeClock, SystemClock
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import Params
from vlah.testing.conformance import ExecutorCheckInputs, check_executor

pytestmark = pytest.mark.executor

COUNT_RUNS_PROGRAM = "open('runs.txt', 'a').write('run\\n')"
IGNORE_SIGTERM_PROGRAM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "open('ready', 'w').close(); time.sleep(600)"
)
DUMP_ENVIRONMENT_PROGRAM = (
    "import json, os; open('environment.json', 'w').write(json.dumps(dict(os.environ)))"
)
INTERRUPT = Interrupt(
    id="i-1",
    agent=WORKER,
    kind="hard",
    rule=2,
    reason="your target claim was refuted",
    written_ts=0.0,
)


def claims_on_blackboard(paths: tuple[str, ...]) -> list[str]:
    return [path for path in paths if path.startswith("claims/")]


def test_check_executor_on_a_fake_clock(tmp_path: Path) -> None:
    clock = FakeClock()
    with running_executor(tmp_path, clock) as harness:
        check_executor(
            harness.executor,
            ExecutorCheckInputs(
                long_running_spec=harness.spec(SLEEP_PROGRAM),
                quick_spec=harness.spec(QUICK_PROGRAM, agent=ActorId("worker-02")),
                task=TASK,
                start_pack=START_PACK,
                heartbeat_interval_seconds=Params().heartbeat_interval_seconds,
            ),
            SteppedTimer(clock=clock, executor=harness.executor),
        )


@pytest.mark.slow
def test_check_executor_in_real_time(tmp_path: Path) -> None:
    params = Params(heartbeat_interval_seconds=1.0)
    with running_executor(
        tmp_path, SystemClock(), params=params, poll_seconds=0.1
    ) as harness:
        check_executor(
            harness.executor,
            ExecutorCheckInputs(
                long_running_spec=harness.spec(SLEEP_PROGRAM),
                quick_spec=harness.spec(QUICK_PROGRAM, agent=ActorId("worker-02")),
                task=TASK,
                start_pack=START_PACK,
                heartbeat_interval_seconds=1.0,
            ),
            RealTimer(),
        )


def test_files_touched_counts_edits_commits_and_new_files(tmp_path: Path) -> None:
    clock = FakeClock()
    with running_executor(tmp_path, clock) as harness:
        harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        target = harness.sandbox() / "target"
        (target / "README.md").write_text("edited\n")
        (target / "committed.py").write_text("x = 1\n")
        git(target, "add", "committed.py")
        git(target, "commit", "-qm", "agent work")
        (target / "new.txt").write_text("new\n")
        clock.advance(Params().heartbeat_interval_seconds)
        harness.executor.tick()
        assert harness.sink.beats[0][1] == (), "the first beat is sent at start"
        assert harness.sink.beats[-1] == (
            WORKER,
            ("README.md", "committed.py", "new.txt"),
            Params().heartbeat_interval_seconds,
        )


def test_the_blackboard_clone_has_no_remote_and_is_refreshed(tmp_path: Path) -> None:
    clock = FakeClock()
    with running_executor(tmp_path, clock) as harness:
        harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        clone = harness.sandbox() / "blackboard"
        assert (clone / "spec.md").read_text() == "the spec"
        assert git(clone, "remote") == "", "the sandbox is never handed the bare repo"
        harness.blackboard.write(ActorId("human"), {"approaches/a-01.md": b"a"}, "a")
        clock.advance(60.0)
        harness.executor.tick()
        assert (clone / "approaches" / "a-01.md").read_text() == "a"


def test_an_exit_without_a_claim_restarts_exactly_once(tmp_path: Path) -> None:
    with running_executor(tmp_path, FakeClock()) as harness:
        handle = harness.executor.start(
            harness.spec(COUNT_RUNS_PROGRAM), TASK, START_PACK, harness.sink
        )
        exit_info = harness.executor.wait(handle, 10.0)
        assert exit_info is not None and exit_info.return_code == 0
        sandbox = harness.sandbox()
        assert (sandbox / "runs.txt").read_text() == "run\nrun\n"
        assert RESTART_HINT in (sandbox / "CONTEXT.md").read_text()
        paths = harness.blackboard.list_paths("", None)
        assert {
            "traces/worker-01/t-00001.ptr",
            "traces/worker-01/t-00001.2.ptr",
        } <= set(paths)
        (claim_path,) = claims_on_blackboard(paths)
        claim = parse_claim(harness.blackboard.read(claim_path, None).decode())
        assert (claim.type, claim.tags, claim.author) == (
            "dead_end",
            (STUB_TAG,),
            WORKER,
        )
        assert claim.evidence[0].trace == "traces/worker-01/t-00001.2.ptr"
        assert [usage.attempt for usage in harness.executor.usage(handle)] == [1, 2]
        assert all(usage.max_rss_bytes > 0 for usage in harness.executor.usage(handle))


def test_an_exit_with_a_claim_ends_the_session(tmp_path: Path) -> None:
    with running_executor(tmp_path, FakeClock()) as harness:
        harness.claims.written[(WORKER, TASK.id)] = ("c-worker-01-00001",)
        handle = harness.executor.start(
            harness.spec(COUNT_RUNS_PROGRAM), TASK, START_PACK, harness.sink
        )
        assert harness.executor.wait(handle, 10.0) is not None
        assert (harness.sandbox() / "runs.txt").read_text() == "run\n"
        assert claims_on_blackboard(harness.blackboard.list_paths("", None)) == []


def test_an_interrupt_restarts_the_task_with_the_interrupt(tmp_path: Path) -> None:
    with running_executor(tmp_path, FakeClock()) as harness:
        handle = harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        restarted = harness.executor.interrupt(handle, INTERRUPT)
        assert restarted.handle_id != handle.handle_id
        old_exit = harness.executor.wait(handle, 0.0)
        assert old_exit is not None and old_exit.was_killed
        assert harness.executor.is_alive(restarted)
        assert harness.executor.session(restarted).start_pack.interrupt == INTERRUPT
        context = (harness.sandbox() / "CONTEXT.md").read_text()
        assert context.startswith("# Task") and INTERRUPT.reason in context
        assert len(claims_on_blackboard(harness.blackboard.list_paths("", None))) == 1


def test_an_agent_that_ignores_sigterm_is_killed_after_the_grace_period(
    tmp_path: Path,
) -> None:
    with running_executor(tmp_path, FakeClock(), grace_seconds=0.2) as harness:
        handle = harness.executor.start(
            harness.spec(IGNORE_SIGTERM_PROGRAM), TASK, START_PACK, harness.sink
        )
        wait_for_file(harness.sandbox() / "ready")
        process_id = harness.executor.process_id(handle)
        harness.executor.interrupt(handle, INTERRUPT)
        old_exit = harness.executor.wait(handle, 0.0)
        assert old_exit is not None and old_exit.was_killed
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, 0)


def test_the_worker_environment_holds_no_push_token(tmp_path: Path) -> None:
    inherited = {"PATH": os.environ.get("PATH", ""), "VLAH_PUSH_TOKEN": "s3cret"}
    with running_executor(
        tmp_path, FakeClock(), inherited_environment=inherited
    ) as harness:
        harness.claims.written[(WORKER, TASK.id)] = ("c-worker-01-00001",)
        handle = harness.executor.start(
            harness.spec(DUMP_ENVIRONMENT_PROGRAM), TASK, START_PACK, harness.sink
        )
        assert harness.executor.wait(handle, 10.0) is not None
        environment = json.loads((harness.sandbox() / "environment.json").read_text())
        assert "VLAH_PUSH_TOKEN" not in environment
        assert "s3cret" not in json.dumps(environment)
        assert environment["VLAH_AGENT"] == WORKER
        assert environment["VLAH_TRANSCRIPT"] == str(
            harness.executor.transcript_path(handle)
        )
        with pytest.raises(ValueError, match="VLAH_PUSH_TOKEN"):
            harness.executor.start(
                harness.spec(QUICK_PROGRAM, env={"VLAH_PUSH_TOKEN": "s3cret"}),
                TASK,
                START_PACK,
                harness.sink,
            )


def test_a_sandbox_inside_the_run_directory_is_refused(tmp_path: Path) -> None:
    with running_executor(
        tmp_path, FakeClock(), sandbox_root=tmp_path / "runs" / "run-1" / "sandboxes"
    ) as harness:
        with pytest.raises(ValueError, match="run directory"):
            harness.executor.start(
                harness.spec(QUICK_PROGRAM), TASK, START_PACK, harness.sink
            )


def process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    else:
        return True


def test_a_killed_group_takes_the_agents_children_with_it(tmp_path: Path) -> None:
    child_program = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
        "open('child.pid', 'w').write(str(child.pid)); time.sleep(600)"
    )
    with running_executor(tmp_path, FakeClock()) as harness:
        handle = harness.executor.start(
            harness.spec(child_program), TASK, START_PACK, harness.sink
        )
        wait_for_file(harness.sandbox() / "child.pid")
        child_id = int((harness.sandbox() / "child.pid").read_text())
        harness.executor.kill(handle)
        # The orphaned child is reaped by init, which can take a moment.
        for _ in range(500):
            if process_exists(child_id):
                time.sleep(0.01)
            else:
                break
        assert not process_exists(child_id)
