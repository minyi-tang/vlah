"""An Executor-port observer; it records lifecycle metadata, never transcripts or state blobs."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ui.replay_recording import TraceRecorder
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.tasks import Task
from vlah.ports.executor import (
    AgentSpec,
    Executor,
    ExitInfo,
    Handle,
    HeartbeatSink,
    StartPack,
)

logger = logging.getLogger(__name__)


@dataclass
class ObservedExecutor:
    executor: Executor
    recorder: TraceRecorder
    tasks: dict[str, Task] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    observations: int = 0

    def observe_start(self, handle: Handle, task: Task) -> None:
        with self.recorder.lock:
            self.observations += 1
            label = f"{handle.handle_id}/observation-{self.observations}"
            self.tasks[task.id] = task
            self.labels[handle.handle_id] = label
            try:
                self.recorder.begin(handle.agent, task, label)
            except (OSError, ValueError, RuntimeError):
                logger.exception(
                    f"could not record session {handle.handle_id}; executor control continues unchanged"
                )

    def observe_end(self, handle: Handle, expected_label: str | None) -> None:
        with self.recorder.lock:
            active = self.recorder.active.get(handle.agent)
            if active is not None and active.id == expected_label:
                try:
                    self.recorder.finish(handle.agent)
                except (OSError, ValueError, RuntimeError):
                    logger.exception(
                        f"could not record the end of {handle.handle_id}; executor control continues unchanged"
                    )
            else:
                pass

    def start(
        self,
        spec: AgentSpec,
        task: Task,
        start_pack: StartPack,
        heartbeat_sink: HeartbeatSink,
    ) -> Handle:
        handle = self.executor.start(spec, task, start_pack, heartbeat_sink)
        self.observe_start(handle, task)
        return handle

    def is_alive(self, handle: Handle) -> bool:
        label = self.labels.get(handle.handle_id)
        alive = self.executor.is_alive(handle)
        if not alive:
            self.observe_end(handle, label)
        else:
            pass
        return alive

    def interrupt(self, handle: Handle, interrupt: Interrupt) -> Handle:
        label = self.labels.get(handle.handle_id)
        replacement = self.executor.interrupt(handle, interrupt)
        self.observe_end(handle, label)
        self.observe_start(replacement, self.tasks[handle.task_id])
        return replacement

    def kill(self, handle: Handle) -> None:
        label = self.labels.get(handle.handle_id)
        self.executor.kill(handle)
        if not self.executor.is_alive(handle):
            self.observe_end(handle, label)
        else:
            pass

    def wait(self, handle: Handle, timeout_seconds: float | None) -> ExitInfo | None:
        label = self.labels.get(handle.handle_id)
        result = self.executor.wait(handle, timeout_seconds)
        if result is not None:
            self.observe_end(handle, label)
        else:
            pass
        return result

    def sandbox_path(self, agent: ActorId, task_id: str) -> Path:
        return self.executor.sandbox_path(agent, task_id)

    def transcript_path(self, handle: Handle) -> Path:
        return self.executor.transcript_path(handle)

    def base_revision(self, agent: ActorId, task_id: str) -> str:
        return self.executor.base_revision(agent, task_id)

    def checkpoint(self, handle: Handle) -> bytes:
        return self.executor.checkpoint(handle)

    def restore(self, checkpoint_blob: bytes) -> Handle:
        handle = self.executor.restore(checkpoint_blob)
        task = self.tasks.get(handle.task_id)
        if task is not None:
            self.observe_start(handle, task)
        else:
            logger.warning(
                f"restored session {handle.handle_id} has no observed task; its metadata remains unavailable"
            )
        return handle
