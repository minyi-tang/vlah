"""The stateless worker loop (v2.2 section 7, D4, D35, D60, D77).

    loop:
      task = claim_task()
      start pack by arm (D58)
      start the session; acquire the lease
      while it runs: poll context usage (70% -> restart), deliver overdue interrupts
      on exit: restart from the D77 pack if the context was near its limit;
               otherwise complete the task only if it has a claim; release the lease

The loop owns its session's handle: an overdue interrupt (D60 layer 3) or a context
restart replaces the handle here, never behind the loop's back. The runtime posts
overdue interrupts to an InterruptInbox instead of calling the executor itself. If
the loop itself fails, it kills its session before letting go of the task, so no
session outlives its lease.
"""

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from vlah.core.blackboard.api import Blackboard
from vlah.core.queue.api import Leases, TaskQueue
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.tasks import Task
from vlah.core.worker.restart import (
    RestartParts,
    recorded_interrupts,
    restart_pack,
    working_directory_diff,
)
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
STOPPING_REASON = "the run is stopping"


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

    def base_revision(self, agent: ActorId, task_id: str) -> str:
        """What target/ started from, for the working-directory diff (D77)."""
        ...


class AgentRegistry(Protocol):
    def register_agent(
        self, agent: ActorId, sandbox_root: Path, task_id: str | None = None
    ) -> str:
        """Point eval_request paths at this task's sandbox; count early claims for it."""
        ...


class StartPackBuilder(Protocol):
    def __call__(
        self, agent: ActorId, task: Task, cursor: int
    ) -> tuple[StartPack, int]:
        """The start pack for a task, and the diff cursor for next time."""
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
    # None builds the arm's start pack (D58); the compose pool supplies its own (D74).
    start_packs: StartPackBuilder | None = None


@dataclass(kw_only=True)
class TaskRun:
    """What one task's loop tracks across its sessions."""

    agent: ActorId
    task: Task
    start_pack: StartPack
    handle: Handle
    context_restarts: int = 0
    over_threshold_since: float | None = None
    # The transcript the wrap-up clock refers to; a new one means a new process.
    watched_transcript: Path | None = None
    # An overdue interrupt the loop delivered itself, for the next restart pack.
    last_interrupt: Interrupt | None = None
    # Interrupt ids a restart pack has already carried (D77 part 3).
    presented_interrupts: set[str] = field(default_factory=set[str])


def run_worker(agent: ActorId, deps: WorkerDeps) -> None:
    """Claim and run tasks until stop is set; the worker keeps no state but its cursor."""
    cursor = 0
    while not deps.stop.is_set():
        task = deps.task_queue.claim_task(agent)
        if task is None:
            deps.stop.wait(deps.idle_seconds)
        elif deps.stop.is_set():
            # The stop landed while claiming: hand the task back instead of starting it.
            deps.task_queue.requeue(task.id, STOPPING_REASON)
        else:
            cursor = run_task(agent, task, deps, cursor)


def run_task(agent: ActorId, task: Task, deps: WorkerDeps, cursor: int) -> int:
    """Run one task to its end; return the diff cursor for the next start pack."""
    if deps.start_packs is None:
        start_pack, next_cursor = build_start_pack(agent, task, deps.context, cursor)
    else:
        start_pack, next_cursor = deps.start_packs(agent, task, cursor)
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
    except BaseException:
        # The loop failed, not the agent: the session must not outlive the lease.
        kill_quietly(run, deps)
        raise
    finally:
        finish_task(run, deps)
    return next_cursor


def kill_quietly(run: TaskRun, deps: WorkerDeps) -> None:
    try:
        deps.executor.kill(run.handle)
    except Exception:
        logger.exception(f"could not kill {run.handle.handle_id} after a loop failure")


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
    """Wait on the session; swap in a new one for an interrupt or a context restart.

    Once the run is stopping, no new session starts: the loop waits for the current
    one to end, which the supervisor's kill makes quick.
    """
    while True:
        exit_info = deps.executor.wait(run.handle, deps.poll_seconds)
        if exit_info is not None:
            log_rate_limits(run, deps)
            if deps.stop.is_set():
                return
            elif ended_near_the_limit(run, deps) and should_restart(run):
                replace_session(run, deps, context_restart_pack(run, deps))
                continue
            else:
                return
        else:
            interrupt = None if deps.stop.is_set() else deps.inbox.take(run.agent)
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


def context_fraction(run: TaskRun, deps: WorkerDeps) -> float | None:
    """How full the current session's context is, or None before it has a transcript."""
    transcript = deps.sessions.transcript_path(run.handle)
    if not transcript.is_file():
        return None
    elif transcript != run.watched_transcript:
        # A new transcript is a new process, such as the executor's own restart:
        # the wrap-up clock starts over. A drop within one transcript does not
        # reset it, because that is the harness compacting, not a fresh context.
        run.watched_transcript = transcript
        run.over_threshold_since = None
        return deps.harness.usage(transcript).fraction
    else:
        return deps.harness.usage(transcript).fraction


def watch_context(run: TaskRun, deps: WorkerDeps) -> None:
    """Note when usage passes the threshold; stop an agent that does not wrap up.

    Once a transcript has crossed, its clock runs whatever later readings say: a
    lower reading on the same transcript is the harness compacting (D77).
    """
    fraction = context_fraction(run, deps)
    threshold = deps.context.params.context_restart_fraction
    now_seconds = deps.clock.now_seconds()
    if run.over_threshold_since is not None:
        if now_seconds - run.over_threshold_since >= deps.wrap_up_seconds:
            logger.warning(f"{run.agent} did not wrap up; stopping it for a restart")
            deps.executor.kill(run.handle)
        else:
            pass
    elif fraction is not None and fraction >= threshold:
        logger.info(f"{run.agent} is at {fraction:.0%} of its context window")
        run.over_threshold_since = now_seconds
    else:
        pass


def ended_near_the_limit(run: TaskRun, deps: WorkerDeps) -> bool:
    """Read the transcript at exit: a poll may have missed a quick wrap-up (D77)."""
    fraction = context_fraction(run, deps)
    threshold = deps.context.params.context_restart_fraction
    return run.over_threshold_since is not None or (
        fraction is not None and fraction >= threshold
    )


def restart_interrupt(run: TaskRun, deps: WorkerDeps) -> Interrupt | None:
    """The interrupt the replaced session was given, once: overdue ones the loop
    delivered itself, or the latest one the hook showed the agent (D77 part 3)."""
    recorded = recorded_interrupts(deps.sessions.sandbox_path(run.agent, run.task.id))
    candidates = [*recorded, *([run.last_interrupt] if run.last_interrupt else [])]
    run.last_interrupt = None
    unseen = [
        interrupt
        for interrupt in candidates
        if interrupt.id not in run.presented_interrupts
    ]
    if unseen:
        run.presented_interrupts.update(interrupt.id for interrupt in unseen)
        return unseen[-1]
    else:
        return None


def context_restart_pack(run: TaskRun, deps: WorkerDeps) -> StartPack:
    blackboard = deps.context.blackboard
    claim_ids = deps.tool_server.claims_written(run.agent, run.task.id)
    return restart_pack(
        run.start_pack,
        RestartParts(
            claims=claim_texts(blackboard, run.agent, claim_ids),
            interrupt=restart_interrupt(run, deps),
            working_diff=working_directory_diff(
                deps.sessions.sandbox_path(run.agent, run.task.id) / "target",
                deps.sessions.base_revision(run.agent, run.task.id),
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
    """Release the lease, then complete the task if it has a claim (D4) or requeue it.

    The lease goes first: the queue refuses to requeue a task an agent still holds,
    so that no task ever has two leases.
    """
    has_claim = bool(deps.tool_server.claims_written(run.agent, run.task.id))
    deps.leases.release(run.agent)
    if has_claim:
        deps.task_queue.complete(run.task.id, run.agent)
    else:
        deps.task_queue.requeue(run.task.id, NO_CLAIM_REASON)
