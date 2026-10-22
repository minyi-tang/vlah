"""Scripted agents on a fake clock: the in-memory Executor and Harness (D61).

A scripted program is a generator that yields how many seconds it wants to wait,
for example inside a long sandbox command. The executor moves the fake clock,
sends heartbeats on its own schedule, and resumes programs when their wait ends,
so every run is deterministic.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.tasks import Task
from vlah.ports.errors import NotSupportedError
from vlah.ports.executor import AgentSpec, ExitInfo, Handle, HeartbeatSink, StartPack
from vlah.ports.harness import ContextUsage, HookConfig, ToolEndpoint, Workspace

logger = logging.getLogger(__name__)

SCRIPTED_COMMAND = "scripted"
TOOL_URL_VARIABLE = "VLAH_TOOL_URL"
TOOL_TOKEN_VARIABLE = "VLAH_TOOL_TOKEN"
HERD_DIRECTORY = ".vlah"


@dataclass(kw_only=True)
class ScriptedSession:
    """What a scripted program sees; files_touched is what heartbeats report.

    transcript_path is where the executor says this process's transcript is; a
    program that wants the harness to read something writes it there.
    """

    agent: ActorId
    task: Task
    start_pack: StartPack
    spec: AgentSpec
    clock: FakeClock
    transcript_path: Path
    files_touched: set[str] = field(default_factory=set[str])


class ScriptedProgram(Protocol):
    def __call__(self, session: ScriptedSession) -> Iterator[float]:
        """Run the agent; each yielded number is a wait in seconds."""
        ...


@dataclass(kw_only=True)
class ScriptedProcess:
    handle: Handle
    spec: AgentSpec
    task: Task
    start_pack: StartPack
    session: ScriptedSession
    steps: Iterator[float]
    heartbeat_sink: HeartbeatSink
    wake_at_seconds: float
    next_heartbeat_at_seconds: float
    exit_info: ExitInfo | None = None


class MemoryExecutor:
    """Runs scripted programs named by AgentSpec.command, on a fake clock.

    Nothing is written under sandbox_root unless a program does; the paths only
    tell the worker loop and the tool server where a task's files would be.
    """

    def __init__(
        self,
        *,
        clock: FakeClock,
        programs: dict[str, ScriptedProgram],
        heartbeat_interval_seconds: float,
        sandbox_root: Path,
    ) -> None:
        self.clock = clock
        self.programs = programs
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.sandbox_root = sandbox_root
        self.processes: dict[str, ScriptedProcess] = {}
        self.base_revisions: dict[tuple[ActorId, str], str] = {}
        self.started_count = 0

    def sandbox_path(self, agent: ActorId, task_id: str) -> Path:
        return self.sandbox_root / agent / task_id

    def start(
        self,
        spec: AgentSpec,
        task: Task,
        start_pack: StartPack,
        heartbeat_sink: HeartbeatSink,
    ) -> Handle:
        if len(spec.command) != 2 or spec.command[0] != SCRIPTED_COMMAND:
            raise ValueError(f"not a scripted command: {spec.command}")
        else:
            program = self.programs[spec.command[1]]
            self.started_count += 1
            handle = Handle(
                handle_id=f"memory-{self.started_count:05d}",
                agent=spec.agent,
                task_id=task.id,
            )
            session = ScriptedSession(
                agent=spec.agent,
                task=task,
                start_pack=start_pack,
                spec=spec,
                clock=self.clock,
                transcript_path=self.sandbox_path(spec.agent, task.id)
                / HERD_DIRECTORY
                / f"{handle.handle_id}.transcript.jsonl",
            )
            self.base_revisions[(spec.agent, task.id)] = spec.target_ref
            now_seconds = self.clock.now_seconds()
            process = ScriptedProcess(
                handle=handle,
                spec=spec,
                task=task,
                start_pack=start_pack,
                session=session,
                steps=program(session),
                heartbeat_sink=heartbeat_sink,
                wake_at_seconds=now_seconds,
                next_heartbeat_at_seconds=now_seconds,
            )
            self.processes[handle.handle_id] = process
            self.send_due_heartbeats(process)
            self.resume(process)
            return handle

    def resume(self, process: ScriptedProcess) -> None:
        try:
            wait_seconds = next(process.steps)
        except StopIteration:
            process.exit_info = ExitInfo(return_code=0, was_killed=False)
        except Exception:
            # A failing program is a crashed agent: record exit code 1, as a process would.
            logger.exception(f"scripted program for {process.handle.agent} failed")
            process.exit_info = ExitInfo(return_code=1, was_killed=False)
        else:
            process.wake_at_seconds = self.clock.now_seconds() + max(wait_seconds, 0.0)

    def send_due_heartbeats(self, process: ScriptedProcess) -> None:
        while process.next_heartbeat_at_seconds <= self.clock.now_seconds():
            process.heartbeat_sink.beat(
                process.handle.agent,
                tuple(sorted(process.session.files_touched)),
                self.clock.now_seconds(),
            )
            process.next_heartbeat_at_seconds += self.heartbeat_interval_seconds

    def running_processes(self) -> list[ScriptedProcess]:
        return [
            process for process in self.processes.values() if process.exit_info is None
        ]

    def advance(self, seconds: float) -> None:
        """Move the fake clock forward, sending heartbeats and resuming programs on the way."""
        target_seconds = self.clock.now_seconds() + seconds
        while True:
            running = self.running_processes()
            due_times = [process.wake_at_seconds for process in running] + [
                process.next_heartbeat_at_seconds for process in running
            ]
            next_seconds = min([*due_times, target_seconds])
            if next_seconds > self.clock.now_seconds():
                self.clock.advance(next_seconds - self.clock.now_seconds())
            else:
                pass
            for process in running:
                self.send_due_heartbeats(process)
                if (
                    process.exit_info is None
                    and process.wake_at_seconds <= self.clock.now_seconds()
                ):
                    self.resume(process)
                else:
                    pass
            if self.clock.now_seconds() >= target_seconds and not any(
                process.wake_at_seconds <= self.clock.now_seconds()
                for process in self.running_processes()
            ):
                break
            else:
                pass

    def is_alive(self, handle: Handle) -> bool:
        return self.processes[handle.handle_id].exit_info is None

    def interrupt(self, handle: Handle, interrupt: Interrupt) -> Handle:
        process = self.processes[handle.handle_id]
        self.kill(handle)
        restarted_pack = process.start_pack.model_copy(update={"interrupt": interrupt})
        return self.start(
            process.spec, process.task, restarted_pack, process.heartbeat_sink
        )

    def kill(self, handle: Handle) -> None:
        process = self.processes[handle.handle_id]
        if process.exit_info is None:
            process.exit_info = ExitInfo(return_code=None, was_killed=True)
        else:
            pass

    def wait(self, handle: Handle, timeout_seconds: float | None) -> ExitInfo | None:
        process = self.processes[handle.handle_id]
        if timeout_seconds is not None:
            self.advance(timeout_seconds)
            return process.exit_info
        else:
            while process.exit_info is None:
                self.advance(
                    max(process.wake_at_seconds - self.clock.now_seconds(), 0.0)
                )
            return process.exit_info

    def transcript_path(self, handle: Handle) -> Path:
        return self.processes[handle.handle_id].session.transcript_path

    def base_revision(self, agent: ActorId, task_id: str) -> str:
        """A scripted target has no history, so its base is the ref the spec named."""
        return self.base_revisions[(agent, task_id)]

    def checkpoint(self, handle: Handle) -> bytes:
        raise NotSupportedError("scripted programs cannot be checkpointed")

    def restore(self, checkpoint_blob: bytes) -> Handle:
        raise NotSupportedError("scripted programs cannot be restored")


class ScriptedHarness:
    """Builds AgentSpecs that name a scripted program instead of an LLM agent."""

    def __init__(
        self,
        *,
        default_program: str,
        programs_by_agent: dict[ActorId, str] | None = None,
    ) -> None:
        self.default_program = default_program
        if programs_by_agent is None:
            self.programs_by_agent: dict[ActorId, str] = {}
        else:
            self.programs_by_agent = dict(programs_by_agent)

    @property
    def supports_tool_hooks(self) -> bool:
        return False

    @property
    def supports_checkpoint(self) -> bool:
        return False

    def build(
        self,
        agent: ActorId,
        task: Task,
        start_pack: StartPack,
        tool_endpoint: ToolEndpoint,
        hook_config: HookConfig,
        workspace: Workspace,
    ) -> AgentSpec:
        program_name = self.programs_by_agent.get(agent, self.default_program)
        return AgentSpec(
            agent=agent,
            command=(SCRIPTED_COMMAND, program_name),
            files=start_pack.files,
            env={
                TOOL_URL_VARIABLE: tool_endpoint.url,
                TOOL_TOKEN_VARIABLE: tool_endpoint.bearer_token,
            },
            target_repository=workspace.target_repository,
            target_ref=workspace.target_ref,
            blackboard_clone_url=workspace.blackboard_clone_url,
        )

    def usage(self, transcript_path: Path) -> ContextUsage:
        """Scripted transcripts are a JSON object with used_tokens and window_tokens."""
        return ContextUsage.model_validate_json(
            transcript_path.read_text(encoding="utf-8")
        )

    def recent_tool_calls(self, transcript_path: Path, count: int) -> tuple[str, ...]:
        """A scripted program makes no model calls, so a transcript lists no tools."""
        return ()

    def rate_limit_retries(self, transcript_path: Path) -> int:
        return 0

    def export_state(self, handle: Handle) -> bytes:
        raise NotSupportedError("scripted programs have no conversation to export")

    def import_state(self, state_blob: bytes) -> AgentSpec:
        raise NotSupportedError("scripted programs have no conversation to import")
