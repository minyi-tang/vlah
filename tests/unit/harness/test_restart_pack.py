"""The D77 restart start pack: exactly the five parts, in order."""

import subprocess
from pathlib import Path

import pytest

from tests.fixtures.executor import SLEEP_PROGRAM, START_PACK, TASK
from tests.fixtures.executor import git as executor_git
from tests.fixtures.executor import running_executor
from tests.fixtures.worker import WORKER
from vlah.core.types.clock import FakeClock
from vlah.core.types.interrupts import Interrupt
from vlah.core.worker.restart import RestartParts, restart_pack, working_directory_diff
from vlah.ports.executor import StartPack

pytestmark = pytest.mark.harness

ORIGINAL = StartPack(
    files={"CONTEXT.md": "## Your task\n\nGo faster.\n", "notes.txt": "x"}
)


def git(directory: Path, *arguments: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(directory),
            "-c",
            "user.name=h",
            "-c",
            "user.email=h@h",
            *arguments,
        ],
        check=True,
        capture_output=True,
    )


def test_the_restart_pack_has_the_five_parts(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    git(target, "init", "-q", "-b", "main")
    (target / "solver.py").write_text("SPEED = 1\n")
    git(target, "add", ".")
    git(target, "commit", "-qm", "baseline")
    (target / "solver.py").write_text("SPEED = 2\n")
    (target / "notes.md").write_text("new\n")
    interrupt = Interrupt(
        id="i-7", agent=WORKER, kind="hard", rule=6, reason="rebase", written_ts=0.0
    )
    pack = restart_pack(
        ORIGINAL,
        RestartParts(
            claims=("claim one", "claim two"),
            interrupt=interrupt,
            working_diff=working_directory_diff(target, "main"),
            tool_calls=("Bash {}", "Edit {}"),
        ),
    )
    context = pack.files["CONTEXT.md"]
    headings = [line[3:] for line in context.splitlines() if line.startswith("## ")]
    assert headings == [
        "Your task",
        "Context restart",
        "Your claims from this task",
        "Interrupt",
        "Working directory diff",
        "Your last tool calls",
    ]
    assert "claim one" in context and "claim two" in context
    assert "-SPEED = 1" in context and "+SPEED = 2" in context
    assert "new file: notes.md" in context
    assert "- Bash {}" in context and "- Edit {}" in context
    assert pack.interrupt == interrupt
    assert pack.files["notes.txt"] == "x", "the rest of the original start pack stays"


def test_without_an_interrupt_there_is_no_interrupt_part(tmp_path: Path) -> None:
    pack = restart_pack(
        ORIGINAL,
        RestartParts(
            claims=(), interrupt=None, working_diff="No changes.", tool_calls=()
        ),
    )
    assert "## Interrupt" not in pack.files["CONTEXT.md"]
    assert working_directory_diff(tmp_path / "missing", "main") == (
        "There is no working directory yet."
    )


def test_the_diff_uses_the_executors_base_not_the_branch(tmp_path: Path) -> None:
    """target/ is a detached clone with no remote, so a branch name may not resolve
    in it; the executor names the base."""
    with running_executor(tmp_path, FakeClock()) as harness:
        executor_git(harness.target, "branch", "baseline-3")
        spec = harness.spec(SLEEP_PROGRAM).model_copy(
            update={"target_ref": "baseline-3"}
        )
        harness.executor.start(spec, TASK, START_PACK, harness.sink)
        target = harness.sandbox() / "target"
        (target / "README.md").write_text("edited by the agent\n")
        executor_git(target, "commit", "-qam", "agent work")
        (target / "README.md").write_text("edited again\n")
        by_branch = working_directory_diff(target, "baseline-3")
        by_base = working_directory_diff(
            target, harness.executor.base_revision(WORKER, TASK.id)
        )
    assert "+edited again" not in by_branch, "the branch is gone from the clone"
    assert "-target" in by_base and "+edited again" in by_base
