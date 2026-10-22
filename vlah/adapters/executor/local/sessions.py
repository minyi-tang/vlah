"""What the executor tracks per handle, and what it asks the tool server."""

import threading
from dataclasses import dataclass, field
from typing import Protocol

from vlah.adapters.executor.local.processes import AgentProcess
from vlah.adapters.executor.local.workspace import SandboxPaths
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.ports.executor import AgentSpec, ExitInfo, Handle, HeartbeatSink, StartPack


class ClaimLedger(Protocol):
    def claims_written(self, agent: ActorId, task_id: str) -> tuple[str, ...]:
        """Claim ids the agent wrote through the tool server during the task (D4)."""
        ...


@dataclass(kw_only=True)
class Session:
    """One handle: a task run by one process, or two when the first exits without a claim."""

    handle: Handle
    spec: AgentSpec
    task: Task
    start_pack: StartPack
    heartbeat_sink: HeartbeatSink
    paths: SandboxPaths
    base_commit: str
    process: AgentProcess
    next_heartbeat_seconds: float
    next_refresh_seconds: float
    last_files_touched: tuple[str, ...] = ()
    stop_reason: str | None = None
    exit_info: ExitInfo | None = None
    ended_processes: list[AgentProcess] = field(default_factory=list[AgentProcess])
    lock: threading.Lock = field(default_factory=threading.Lock)
    finished: threading.Event = field(default_factory=threading.Event)
