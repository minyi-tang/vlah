"""The local executor: each worker is a subprocess in its own sandbox and process group (D32, D60).

A sidecar thread calls tick on a real-time poll. tick does whatever the injected
clock says is due for each session:
- a heartbeat with files_touched, on its own schedule, whatever the agent is doing;
- a refresh of the blackboard clone;
- noticing a process that ended, archiving its trace, and restarting it once when
  it exited without a claim (otherwise a stub claim, D77).
Waits for a process (wait, the interrupt grace period) count real seconds and never
read the clock, so the executor also runs on a fake clock in tests.
"""

import logging
import math
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

from vlah.adapters.executor.local.processes import (
    REAP_POLL_SECONDS,
    AgentProcess,
    ResourceUsage,
    kill,
    launch_process,
    reap,
    stop,
)
from vlah.adapters.executor.local.sessions import ClaimLedger, Session
from vlah.adapters.executor.local.traces import (
    RESTART_HINT,
    EndedSession,
    SessionLogs,
    interrupt_notice,
    with_notice,
    write_trace,
)
from vlah.adapters.executor.local.workspace import (
    GitCommandError,
    SandboxPaths,
    check_out_target,
    clone_blackboard,
    files_touched,
    refresh_blackboard,
    refuse_sandbox_inside_run_directory,
    sandbox_paths,
    worker_environment,
    write_start_files,
)
from vlah.adapters.executor.sandbox.api import Sandbox
from vlah.core.blackboard.api import Blackboard
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import EgressPolicy, Params
from vlah.core.types.tasks import Task
from vlah.ports.errors import NotSupportedError
from vlah.ports.executor import AgentSpec, ExitInfo, Handle, HeartbeatSink, StartPack
from vlah.ports.store import ObjectStore

logger = logging.getLogger(__name__)

DEFAULT_GRACE_SECONDS = 30.0
DEFAULT_CLONE_REFRESH_SECONDS = 60.0
DEFAULT_POLL_SECONDS = 1.0
SIDECAR_FAILURES = (GitCommandError, subprocess.TimeoutExpired, OSError)


class LocalExecutor:
    def __init__(
        self,
        *,
        sandbox_root: Path,
        log_root: Path,
        blackboard: Blackboard,
        objects: ObjectStore,
        claims: ClaimLedger,
        sandbox: Sandbox,
        params: Params,
        clock: Clock,
        inherited_environment: Mapping[str, str],
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        clone_refresh_seconds: float = DEFAULT_CLONE_REFRESH_SECONDS,
        poll_seconds: float | None = DEFAULT_POLL_SECONDS,
    ) -> None:
        """poll_seconds None runs no sidecar thread; the caller then drives tick itself."""
        self.sandbox_root = sandbox_root
        self.log_root = log_root
        self.blackboard = blackboard
        self.objects = objects
        self.claims = claims
        self.sandbox = sandbox
        self.params = params
        self.clock = clock
        self.inherited_environment = dict(inherited_environment)
        self.grace_seconds = grace_seconds
        self.clone_refresh_seconds = clone_refresh_seconds
        self.sessions: dict[str, Session] = {}
        self.base_commits: dict[Path, str] = {}
        self.started_count = 0
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        if poll_seconds is None:
            self.sidecar: threading.Thread | None = None
        else:
            self.sidecar = threading.Thread(
                target=self.run_sidecar,
                args=(poll_seconds,),
                name="executor-sidecar",
                daemon=True,
            )
            self.sidecar.start()

    def sandbox_path(self, agent: ActorId, task_id: str) -> Path:
        """Where a task's sandbox lives; the runtime registers it with the tool server."""
        return self.sandbox_root / agent / task_id

    def start(
        self,
        spec: AgentSpec,
        task: Task,
        start_pack: StartPack,
        heartbeat_sink: HeartbeatSink,
    ) -> Handle:
        refuse_sandbox_inside_run_directory(
            self.sandbox_root, spec.blackboard_clone_url
        )
        paths = sandbox_paths(self.sandbox_path(spec.agent, task.id))
        paths.root.mkdir(parents=True, exist_ok=True)
        with self.lock:
            known_base_commit = self.base_commits.get(paths.root)
        base_commit = known_base_commit or check_out_target(paths, spec)
        with self.lock:
            self.base_commits[paths.root] = base_commit
            self.started_count += 1
            handle = Handle(
                handle_id=f"local-{self.started_count:05d}",
                agent=spec.agent,
                task_id=task.id,
            )
        clone_blackboard(paths, spec.blackboard_clone_url)
        process = self.launch(
            handle, spec, paths, {**spec.files, **start_pack.files}, attempt=1
        )
        now_seconds = self.clock.now_seconds()
        session = Session(
            handle=handle,
            spec=spec,
            task=task,
            start_pack=start_pack,
            heartbeat_sink=heartbeat_sink,
            paths=paths,
            base_commit=base_commit,
            process=process,
            next_heartbeat_seconds=now_seconds,
            next_refresh_seconds=now_seconds + self.clone_refresh_seconds,
        )
        # The first beat goes out before the sidecar can see the session, so it is sent once.
        with session.lock:
            self.send_heartbeat(session, now_seconds)
        with self.lock:
            self.sessions[handle.handle_id] = session
        return handle

    def launch(
        self,
        handle: Handle,
        spec: AgentSpec,
        paths: SandboxPaths,
        files: dict[str, str],
        attempt: int,
    ) -> AgentProcess:
        write_start_files(paths.root, files)
        log_directory = (
            self.log_root
            / handle.agent
            / handle.task_id
            / handle.handle_id
            / str(attempt)
        )
        log_directory.mkdir(parents=True, exist_ok=True)
        transcript = paths.transcript(handle.handle_id, attempt)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        logs = SessionLogs(
            stdout=log_directory / "stdout.log",
            stderr=log_directory / "stderr.log",
            transcript=transcript,
        )
        environment = worker_environment(
            spec, handle.task_id, paths, transcript, self.inherited_environment
        )
        wrapped = self.sandbox.wrap(
            spec.agent, spec.command, environment, spec.egress or EgressPolicy()
        )
        return launch_process(wrapped, paths.root, logs, attempt, self.clock)

    def session(self, handle: Handle) -> Session:
        with self.lock:
            if handle.handle_id not in self.sessions:
                raise ValueError(f"unknown handle {handle.handle_id}")
            else:
                return self.sessions[handle.handle_id]

    def run_sidecar(self, poll_seconds: float) -> None:
        while not self.stopping.wait(poll_seconds):
            try:
                self.tick()
            except Exception:
                logger.exception("the executor sidecar failed; it keeps running")

    def tick(self) -> None:
        """Do everything that is due now; a session busy elsewhere is skipped until next time."""
        now_seconds = self.clock.now_seconds()
        with self.lock:
            sessions = [
                session
                for session in self.sessions.values()
                if not session.finished.is_set()
            ]
        for session in sessions:
            if session.lock.acquire(blocking=False):
                try:
                    self.service(session, now_seconds)
                finally:
                    session.lock.release()
            else:
                pass

    def service(self, session: Session, now_seconds: float) -> None:
        """With session.lock held: notice an exit, or send what is due."""
        if session.finished.is_set():
            return
        else:
            exit_info = self.reap(session, blocking=False)
            if exit_info is not None:
                self.after_exit(session, exit_info)
            else:
                if now_seconds >= session.next_heartbeat_seconds:
                    self.send_heartbeat(session, now_seconds)
                else:
                    pass
                if now_seconds >= session.next_refresh_seconds:
                    self.refresh(session, now_seconds)
                else:
                    pass

    def send_heartbeat(self, session: Session, now_seconds: float) -> None:
        """A failed git diff still sends the beat, with the last known files (D32)."""
        try:
            session.last_files_touched = files_touched(
                session.paths, session.base_commit
            )
        except SIDECAR_FAILURES:
            logger.exception(f"files_touched failed for {session.handle.agent}")
        try:
            session.heartbeat_sink.beat(
                session.handle.agent, session.last_files_touched, now_seconds
            )
        except Exception:
            logger.exception(f"heartbeat failed for {session.handle.agent}")
        session.next_heartbeat_seconds = (
            now_seconds + self.params.heartbeat_interval_seconds
        )

    def refresh(self, session: Session, now_seconds: float) -> None:
        try:
            refresh_blackboard(session.paths, session.spec.blackboard_clone_url)
        except SIDECAR_FAILURES:
            logger.exception(f"blackboard refresh failed for {session.handle.agent}")
        session.next_refresh_seconds = now_seconds + self.clone_refresh_seconds

    def reap(self, session: Session, blocking: bool) -> ExitInfo | None:
        return reap(session.process, blocking, self.clock)

    def after_exit(self, session: Session, exit_info: ExitInfo) -> None:
        """Archive the trace; then restart once, or finish with a stub when there is no claim."""
        handle = session.handle
        has_claim = bool(self.claims.claims_written(handle.agent, handle.task_id))
        if session.stop_reason is not None:
            reason, should_restart = session.stop_reason, False
        elif exit_info.was_killed:
            reason, should_restart = "killed from outside the herd", False
        elif has_claim:
            reason, should_restart = "finished", False
        elif session.process.attempt == 1:
            reason, should_restart = "no claim yet; restarting once with a hint", True
        else:
            reason, should_restart = "no claim after the restart", False
        ended = EndedSession(
            agent=handle.agent,
            task=session.task,
            handle_id=handle.handle_id,
            attempt=session.process.attempt,
            exit_info=exit_info,
            reason=reason,
            logs=session.process.logs,
        )
        restarted = self.restart_with_hint(session) if should_restart else None
        write_trace(
            self.blackboard,
            self.objects,
            ended,
            needs_stub=not has_claim and restarted is None,
        )
        if restarted is not None:
            session.ended_processes.append(session.process)
            session.process = restarted
        else:
            session.exit_info = exit_info
            session.finished.set()

    def restart_with_hint(self, session: Session) -> AgentProcess | None:
        files = with_notice(
            {**session.spec.files, **session.start_pack.files}, "Restart", RESTART_HINT
        )
        try:
            return self.launch(
                session.handle, session.spec, session.paths, files, attempt=2
            )
        except (OSError, ValueError, RuntimeError):
            logger.exception(f"could not restart {session.handle.agent}")
            return None

    def is_alive(self, handle: Handle) -> bool:
        session = self.session(handle)
        if session.lock.acquire(blocking=False):
            try:
                self.service(session, self.clock.now_seconds())
            finally:
                session.lock.release()
        else:
            pass
        return not session.finished.is_set()

    def interrupt(self, handle: Handle, interrupt: Interrupt) -> Handle:
        """SIGTERM, wait out the grace period, SIGKILL; then start again with the interrupt."""
        session = self.session(handle)
        with session.lock:
            if not session.finished.is_set():
                session.stop_reason = f"stopped by interrupt {interrupt.id}"
                exit_info = stop(session.process, self.grace_seconds, self.clock)
                self.after_exit(session, exit_info)
            else:
                pass
        restarted_pack = session.start_pack.model_copy(
            update={
                "interrupt": interrupt,
                "files": with_notice(
                    dict(session.start_pack.files),
                    "Interrupt",
                    interrupt_notice(interrupt),
                ),
            }
        )
        return self.start(
            session.spec, session.task, restarted_pack, session.heartbeat_sink
        )

    def kill(self, handle: Handle) -> None:
        session = self.session(handle)
        with session.lock:
            if session.finished.is_set():
                return
            else:
                session.stop_reason = "killed by the herd"
                self.after_exit(session, kill(session.process, self.clock))

    def wait(self, handle: Handle, timeout_seconds: float | None) -> ExitInfo | None:
        """Wait in real seconds; this executor's own polls notice the exit."""
        session = self.session(handle)
        if timeout_seconds is None:
            step_count = None
        else:
            step_count = max(1, math.ceil(timeout_seconds / REAP_POLL_SECONDS))
        steps_taken = 0
        while step_count is None or steps_taken < step_count:
            if not self.is_alive(handle):
                return session.exit_info
            else:
                session.finished.wait(REAP_POLL_SECONDS)
                steps_taken += 1
        if self.is_alive(handle):
            return None
        else:
            return session.exit_info

    def checkpoint(self, handle: Handle) -> bytes:
        raise NotSupportedError("local checkpoints wait for the H11 spike (#24)")

    def restore(self, checkpoint_blob: bytes) -> Handle:
        raise NotSupportedError("local checkpoints wait for the H11 spike (#24)")

    def process_id(self, handle: Handle) -> int:
        """The current process's id, which is also its process group id."""
        return self.session(handle).process.popen.pid

    def transcript_path(self, handle: Handle) -> Path:
        """Where the current process's harness writes its transcript (VLAH_TRANSCRIPT)."""
        return self.session(handle).process.logs.transcript

    def usage(self, handle: Handle) -> tuple[ResourceUsage, ...]:
        session = self.session(handle)
        return tuple(
            process.usage
            for process in (*session.ended_processes, session.process)
            if process.usage is not None
        )

    def close(self) -> None:
        """Stop the sidecar and kill every session still running."""
        self.stopping.set()
        if self.sidecar is not None:
            self.sidecar.join()
        else:
            pass
        with self.lock:
            handles = [session.handle for session in self.sessions.values()]
        for handle in handles:
            self.kill(handle)
