"""Executor port: where agent processes run (v2.2 section 4.1, D32, D60).

Heartbeats come from the executor, never from the agent, so a worker that sits
in a long sandbox command never looks dead.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vlah.core.types.base import FrozenModel
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import EgressPolicy
from vlah.core.types.tasks import Task


class StartPack(FrozenModel):
    """Files an agent session starts from, such as CONTEXT.md (D77)."""

    files: dict[str, str]
    interrupt: Interrupt | None = None


class AgentSpec(FrozenModel):
    """Everything an executor needs to launch one agent session."""

    agent: ActorId
    command: tuple[str, ...]
    files: dict[str, str] = {}
    env: dict[str, str] = {}
    target_repository: str
    target_ref: str
    blackboard_clone_url: str
    egress: EgressPolicy | None = None


@dataclass(frozen=True, kw_only=True)
class Handle:
    handle_id: str
    agent: ActorId
    task_id: str


@dataclass(frozen=True, kw_only=True)
class ExitInfo:
    """How a session ended; return_code is None when it was killed by a signal."""

    return_code: int | None
    was_killed: bool


class HeartbeatSink(Protocol):
    def beat(
        self, agent: ActorId, files_touched: tuple[str, ...], timestamp_seconds: float
    ) -> None:
        """Record that agent's process is alive, with the files it has changed."""
        ...


class Executor(Protocol):
    def start(
        self,
        spec: AgentSpec,
        task: Task,
        start_pack: StartPack,
        heartbeat_sink: HeartbeatSink,
    ) -> Handle: ...

    def is_alive(self, handle: Handle) -> bool: ...

    def interrupt(self, handle: Handle, interrupt: Interrupt) -> Handle:
        """Stop the session (SIGTERM, then kill) and restart it with the interrupt."""
        ...

    def kill(self, handle: Handle) -> None: ...

    def wait(self, handle: Handle, timeout_seconds: float | None) -> ExitInfo | None:
        """Exit information, or None if the session is still running at the timeout."""
        ...

    def sandbox_path(self, agent: ActorId, task_id: str) -> Path:
        """Where a task's sandbox lives; eval_request paths resolve inside it."""
        ...

    def transcript_path(self, handle: Handle) -> Path:
        """Where the session's current process writes the transcript the harness reads."""
        ...

    def base_revision(self, agent: ActorId, task_id: str) -> str:
        """What the task's target/ started from, for the working-directory diff (D77)."""
        ...

    def checkpoint(self, handle: Handle) -> bytes:
        """Raises NotSupportedError until checkpoints exist (H11)."""
        ...

    def restore(self, checkpoint_blob: bytes) -> Handle:
        """Raises NotSupportedError until checkpoints exist (H11)."""
        ...
