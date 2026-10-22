"""The stateless worker loop (v2.2 section 7, D4, D35, D60, D77).

    loop:
      task = claim_task()
      start pack by arm (D58)
      start the session; acquire the lease
      while it runs: poll context usage (70% -> restart), deliver overdue interrupts
      on exit: complete the task only if it has a claim; release the lease

The loop owns its session's handle: an overdue interrupt (D60 layer 3) or a context
restart replaces the handle here, never behind the loop's back. The runtime posts
overdue interrupts to an InterruptInbox instead of calling the executor itself.
"""

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vlah.core.blackboard.api import Blackboard
from vlah.core.queue.api import Leases, TaskQueue
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.tasks import Task
from vlah.core.worker.restart import RestartParts, restart_pack, working_directory_diff
from vlah.core.worker.start_pack import ContextSources, build_start_pack
from vlah.ports.executor import Executor, Handle, StartPack
from vlah.ports.harness import Harness, Workspace
from vlah.tools.api import ToolServer

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 30.0
DEFAULT_IDLE_SECONDS = 30.0
# How long an agent past the context threshold may take to write its claim and end
# before the loop stops it; the executor then writes a stub (D77).
DEFAULT_WRAP_UP_SECONDS = 600.0
MAX_CONTEXT_RESTARTS = 5
NO_CLAIM_REASON = "the session ended without a claim (D4)"


class TranscriptReader(Protocol):
    def recent_tool_calls(self, transcript_path: Path, count: int) -> tuple[str, ...]:
        """The last count tool calls in a transcript, one line each."""
        ...

    def rate_limit_retries(self, transcript_path: Path) -> int:
        """How often the model API throttled the session (C7)."""
        ...


class WorkerHarness(Harness, TranscriptReader, Protocol):
    pass


class SessionFiles(Protocol):
    """Where an executor keeps a task's sandbox and a session's transcript."""

    def sandbox_path(self, agent: ActorId, task_id: str) -> Path: ...

    def transcript_path(self, handle: Handle) -> Path: ...


class AgentRegistry(Protocol):
    def register_agent(
        self, agent: ActorId, sandbox_root: Path, task_id: str | None = None
    ) -> str:
        """Point eval_request paths at this task's sandbox; count early claims for it."""
        ...


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class InterruptInbox:
    """Overdue hard interrupts per agent, posted by the runtime from Router.overdue."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending: defaultdict[ActorId, list[Interrupt]] = defaultdict(list)

    def post(self, interrupt: Interrupt) -> None:
        with self.lock:
            self.pending[interrupt.agent].append(interrupt)

    def take(self, agent: ActorId) -> Interrupt | None:
        with self.lock:
            waiting = self.pending.get(agent)
            if waiting:
                return waiting.pop(0)
            else:
                return None


@dataclass(frozen=True, kw_only=True)
class WorkerDeps:
    task_queue: TaskQueue
    leases: Leases
    executor: Executor
    sessions: SessionFiles
    harness: WorkerHarness
    tool_server: ToolServer
    registry: AgentRegistry
    context: ContextSources
    inbox: InterruptInbox
    workspace: Workspace
    clock: Clock
    stop: StopSignal
    poll_seconds: float = DEFAULT_POLL_SECONDS
    idle_seconds: float = DEFAULT_IDLE_SECONDS
    wrap_up_seconds: float = DEFAULT_WRAP_UP_SECONDS


@dataclass(kw_only=True)
class TaskRun:
    """What one task's loop tracks across its sessions."""

    agent: ActorId
    task: Task
    start_pack: StartPack
    handle: Handle
    context_restarts: int = 0
    over_threshold_since: float | None = None
    last_interrupt: Interrupt | None = None


def run_worker(agent: ActorId, deps: WorkerDeps) -> None:
    """Claim and run tasks until stop is set; the worker keeps no state but its cursor."""
    cursor = 0
    while not deps.stop.is_set():
        task = deps.task_queue.claim_task(agent)
        if task is None:
            deps.stop.wait(deps.idle_seconds)
        else:
            cursor = run_task(agent, task, deps, cursor)


def run_task(agent: ActorId, task: Task, deps: WorkerDeps, cursor: int) -> int:
    """Run one task to its end; return the diff cursor for the next start pack."""
    start_pack, next_cursor = build_start_pack(agent, task, deps.context, cursor)
    try:
        handle = start_session(agent, task, start_pack, deps)
    except Exception:
        logger.exception(f"{agent} could not start {task.id}")
        deps.task_queue.requeue(task.id, "the session could not start")
        return next_cursor
    deps.leases.acquire(agent, task, handle.handle_id)
    run = TaskRun(agent=agent, task=task, start_pack=start_pack, handle=handle)
    try:
        follow_sessions(run, deps)
    finally:
        finish_task(run, deps)
    return next_cursor


def start_session(
    agent: ActorId, task: Task, start_pack: StartPack, deps: WorkerDeps
) -> Handle:
    deps.registry.register_agent(
        agent, deps.sessions.sandbox_path(agent, task.id), task.id
    )
    spec = deps.harness.build(
        agent,
        task,
        start_pack,
        deps.tool_server.endpoint_for(agent),
        deps.tool_server.hook_config_for(agent),
        deps.workspace,
    )
    return deps.executor.start(spec, task, start_pack, deps.leases)


def follow_sessions(run: TaskRun, deps: WorkerDeps) -> None:
    """Wait on the session; swap in a new one for an interrupt or a context restart."""
    while True:
        exit_info = deps.executor.wait(run.handle, deps.poll_seconds)
        if exit_info is not None:
            log_rate_limits(run, deps)
            if run.over_threshold_since is not None and should_restart(run):
                replace_session(run, deps, context_restart_pack(run, deps))
                continue
            else:
                return
        else:
            interrupt = deps.inbox.take(run.agent)
            if interrupt is not None:
                run.last_interrupt = interrupt
                run.over_threshold_since = None
                run.handle = deps.executor.interrupt(run.handle, interrupt)
                move_lease(run, deps)
            else:
                watch_context(run, deps)


def log_rate_limits(run: TaskRun, deps: WorkerDeps) -> None:
    """The 429 count per session, a standing diagnostic (section 13)."""
    retries = deps.harness.rate_limit_retries(deps.sessions.transcript_path(run.handle))
    logger.info(
        f"{run.agent} session {run.handle.handle_id} on {run.task.id}: "
        f"{retries} rate-limit retries"
    )


def should_restart(run: TaskRun) -> bool:
    if run.context_restarts >= MAX_CONTEXT_RESTARTS:
        logger.warning(f"{run.agent} reached {MAX_CONTEXT_RESTARTS} context restarts")
        return False
    else:
        return True


def watch_context(run: TaskRun, deps: WorkerDeps) -> None:
    """Note when usage passes the threshold; stop an agent that does not wrap up.

    Within one process the context only grows, so usage back under the threshold
    means the process was replaced, for example by the executor's own restart, and
    the wrap-up clock starts over.
    """
    transcript = deps.sessions.transcript_path(run.handle)
    if not transcript.is_file():
        return
    else:
        fraction = deps.harness.usage(transcript).fraction
        now_seconds = deps.clock.now_seconds()
        threshold = deps.context.params.context_restart_fraction
        if fraction < threshold:
            run.over_threshold_since = None
        elif run.over_threshold_since is None:
            logger.info(f"{run.agent} is at {fraction:.0%} of its context window")
            run.over_threshold_since = now_seconds
        elif now_seconds - run.over_threshold_since >= deps.wrap_up_seconds:
            logger.warning(f"{run.agent} did not wrap up; stopping it for a restart")
            deps.executor.kill(run.handle)
        else:
            pass


def context_restart_pack(run: TaskRun, deps: WorkerDeps) -> StartPack:
    blackboard = deps.context.blackboard
    claim_ids = deps.tool_server.claims_written(run.agent, run.task.id)
    return restart_pack(
        run.start_pack,
        RestartParts(
            claims=claim_texts(blackboard, run.agent, claim_ids),
            interrupt=run.last_interrupt,
            working_diff=working_directory_diff(
                deps.sessions.sandbox_path(run.agent, run.task.id) / "target",
                deps.workspace.target_ref,
            ),
            tool_calls=deps.harness.recent_tool_calls(
                deps.sessions.transcript_path(run.handle),
                deps.context.params.restart_tool_call_count,
            ),
        ),
    )


def claim_texts(
    blackboard: Blackboard, agent: ActorId, claim_ids: tuple[str, ...]
) -> tuple[str, ...]:
    texts: list[str] = []
    for claim_id in claim_ids:
        path = f"claims/{agent}/{claim_id}.md"
        if path in blackboard.list_paths(path, None):
            texts.append(blackboard.read(path, None).decode("utf-8", "replace").strip())
        else:
            texts.append(f"{claim_id} (not merged yet)")
    return tuple(texts)


def replace_session(run: TaskRun, deps: WorkerDeps, start_pack: StartPack) -> None:
    run.context_restarts += 1
    run.over_threshold_since = None
    run.handle = start_session(run.agent, run.task, start_pack, deps)
    move_lease(run, deps)


def move_lease(run: TaskRun, deps: WorkerDeps) -> None:
    """Point the lease at the new handle and keep the targets the task gathered (D55)."""
    previous = deps.leases.get(run.agent)
    deps.leases.release(run.agent)
    deps.leases.acquire(run.agent, run.task, run.handle.handle_id)
    if previous is not None:
        deps.leases.add_targets(run.agent, previous.target_claims, previous.depends_on)
    else:
        pass


def finish_task(run: TaskRun, deps: WorkerDeps) -> None:
    """Complete the task only if it has a claim (D4); release the lease either way."""
    try:
        if deps.tool_server.claims_written(run.agent, run.task.id):
            deps.task_queue.complete(run.task.id, run.agent)
        else:
            deps.task_queue.requeue(run.task.id, NO_CLAIM_REASON)
    finally:
        deps.leases.release(run.agent)
