"""The local executor on a real git blackboard, with Python one-liners as agents."""

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from vlah.adapters.executor.local.executor import LocalExecutor
from vlah.adapters.executor.local.passthrough import PassthroughSandbox
from vlah.adapters.memory.store import MemoryObjectStore
from vlah.core.blackboard.api import Blackboard
from vlah.core.blackboard.layout import open_blackboard
from vlah.core.types.clock import Clock, FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task
from vlah.ports.executor import AgentSpec, StartPack
from vlah.testing.conformance import RecordingHeartbeatSink

WORKER = ActorId("worker-01")
TASK = Task(
    id="t-00001", approach="a-01", kind="advance", goal="Advance", created_by="queue"
)
START_PACK = StartPack(files={"CONTEXT.md": "# Task\n\nAdvance a-01."})
SLEEP_PROGRAM = "import time; time.sleep(600)"
QUICK_PROGRAM = "pass"
GIT_IDENTITY = ("-c", "user.name=herd", "-c", "user.email=herd@vlah")
READY_TIMEOUT_SECONDS = 10.0


def git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *GIT_IDENTITY, *arguments],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def make_target_repository(path: Path) -> Path:
    """A one-commit target system with README.md on main."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("target\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "baseline")
    return path


@dataclass(kw_only=True)
class RecordingClaims:
    """Stands in for the tool server's record of claims written per task."""

    written: dict[tuple[ActorId, str], tuple[str, ...]] = field(
        default_factory=dict[tuple[ActorId, str], tuple[str, ...]]
    )

    def claims_written(self, agent: ActorId, task_id: str) -> tuple[str, ...]:
        return self.written.get((agent, task_id), ())


@dataclass(kw_only=True)
class ExecutorHarness:
    executor: LocalExecutor
    blackboard: Blackboard
    objects: MemoryObjectStore
    claims: RecordingClaims
    sink: RecordingHeartbeatSink
    target: Path
    clone_url: str

    def spec(
        self,
        program: str,
        agent: ActorId = WORKER,
        env: dict[str, str] | None = None,
    ) -> AgentSpec:
        return AgentSpec(
            agent=agent,
            command=(sys.executable, "-c", program),
            env=env or {},
            target_repository=str(self.target),
            target_ref="main",
            blackboard_clone_url=self.clone_url,
        )

    def sandbox(self, agent: ActorId = WORKER, task_id: str = TASK.id) -> Path:
        return self.executor.sandbox_path(agent, task_id)


@dataclass(frozen=True, kw_only=True)
class SteppedTimer:
    """A CheckTimer for a fake clock: it moves the clock a second at a time and ticks."""

    clock: FakeClock
    executor: LocalExecutor
    step_seconds: float = 1.0

    def wait_seconds(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0:
            step = min(self.step_seconds, remaining)
            self.clock.advance(step)
            self.executor.tick()
            remaining -= step


class RealTimer:
    def wait_seconds(self, seconds: float) -> None:
        time.sleep(seconds)


def wait_for_file(path: Path) -> None:
    """Let a real agent process get going; bounded so a broken test fails instead of hanging."""
    for _ in range(int(READY_TIMEOUT_SECONDS / 0.01)):
        if path.exists():
            return
        else:
            time.sleep(0.01)
    raise AssertionError(f"{path} never appeared")


@contextmanager
def running_executor(
    tmp_path: Path,
    clock: Clock,
    params: Params | None = None,
    poll_seconds: float | None = None,
    grace_seconds: float = 0.5,
    inherited_environment: dict[str, str] | None = None,
    sandbox_root: Path | None = None,
    objects: MemoryObjectStore | None = None,
) -> Iterator[ExecutorHarness]:
    blackboard = open_blackboard(tmp_path / "runs", "run-1", "blackboard")
    blackboard.write(ActorId("human"), {"spec.md": b"the spec"}, "spec")
    objects = objects or MemoryObjectStore()
    claims = RecordingClaims()
    executor = LocalExecutor(
        sandbox_root=sandbox_root or tmp_path / "sandboxes",
        log_root=tmp_path / "logs",
        blackboard=blackboard,
        objects=objects,
        claims=claims,
        sandbox=PassthroughSandbox(),
        params=params or Params(),
        clock=clock,
        inherited_environment=(
            inherited_environment
            if inherited_environment is not None
            else {"PATH": os.environ.get("PATH", "")}
        ),
        grace_seconds=grace_seconds,
        poll_seconds=poll_seconds,
    )
    try:
        yield ExecutorHarness(
            executor=executor,
            blackboard=blackboard,
            objects=objects,
            claims=claims,
            sink=RecordingHeartbeatSink(),
            target=make_target_repository(tmp_path / "target"),
            clone_url=blackboard.clone_url(),
        )
    finally:
        executor.close()
