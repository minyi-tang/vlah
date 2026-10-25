"""The Docker executor: each session is a container whose only mount is its sandbox (D32, D60, D67, D75).

The sandbox stays on the host, at <sandbox root>/<agent>/<task>, bound at /work in
the container, so the sidecar's heartbeat (git diff on the host), the clone refresh
and the trace archiving are the local executor's code unchanged. The container
joins the run's internal network, whose only way out is the run's egress proxy
container (egress.py); every proxy variable points at it and nothing is excluded,
so even the tool server goes through the allowlist. The environment is the
session's own plus what the executor sets: nothing of the host is inherited, and
HOME is inside the sandbox. A sidecar thread polls docker inspect and the clock;
an exit code above 128 or an OOM kill is a killed session.
"""

import logging
import math
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from vlah.adapters.executor.docker.client import (
    BindMount,
    ContainerSpec,
    ContainerState,
    DockerClient,
    DockerCommandError,
)
from vlah.adapters.executor.docker.egress import (
    AGENT_LABEL,
    ATTEMPT_LABEL,
    RUN_LABEL,
    TASK_LABEL,
    RunEgress,
    docker_name,
)
from vlah.adapters.executor.local.sessions import ClaimLedger
from vlah.adapters.executor.local.traces import (
    RESTART_HINT,
    EndedSession,
    SessionLogs,
    interrupt_notice,
    with_notice,
    write_trace,
)
from vlah.adapters.executor.local.workspace import (
    BASE_REF,
    FORBIDDEN_VARIABLES,
    GitCommandError,
    SandboxPaths,
    check_out_target,
    clone_blackboard,
    files_touched,
    herd_variables,
    refresh_blackboard,
    refuse_sandbox_inside_run_directory,
    sandbox_paths,
    write_start_files,
)
from vlah.adapters.executor.sandbox.api import DeniedConnection
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

WORK_DIRECTORY = "/work"
HOME_NAME = ".home"
HOME_DIRECTORY = f"{WORK_DIRECTORY}/{HOME_NAME}"
CONTAINER_PATHS = sandbox_paths(Path(WORK_DIRECTORY))
DEFAULT_CPUS = 2.0
DEFAULT_MEMORY = "2g"
DEFAULT_GRACE_SECONDS = 30.0
DEFAULT_CLONE_REFRESH_SECONDS = 60.0
DEFAULT_POLL_SECONDS = 1.0
# How often wait and the stop paths ask Docker about a container.
EXIT_POLL_SECONDS = 0.25
STOP_SETTLE_SECONDS = 10.0
# Docker reports a signal exit as 128 plus the signal number, as shells do.
SIGNAL_EXIT_BASE = 128
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
NO_PROXY_VARIABLES = ("NO_PROXY", "no_proxy")
SIDECAR_FAILURES = (GitCommandError, subprocess.TimeoutExpired, OSError)


@dataclass(frozen=True, kw_only=True)
class ContainerResources:
    """Limits every worker container gets, as docker run takes them."""

    cpus: float = DEFAULT_CPUS
    memory: str = DEFAULT_MEMORY


@dataclass(kw_only=True)
class AgentContainer:
    container_id: str
    name: str
    attempt: int
    logs: SessionLogs
    exit_info: ExitInfo | None = None


@dataclass(kw_only=True)
class ContainerSession:
    """One handle: a task run by one container, or two when the first exits without a claim."""

    handle: Handle
    spec: AgentSpec
    task: Task
    start_pack: StartPack
    heartbeat_sink: HeartbeatSink
    paths: SandboxPaths
    base_commit: str
    container: AgentContainer
    next_heartbeat_seconds: float
    next_refresh_seconds: float
    last_files_touched: tuple[str, ...] = ()
    stop_reason: str | None = None
    exit_info: ExitInfo | None = None
    ended_containers: list[AgentContainer] = field(default_factory=list[AgentContainer])
    lock: threading.Lock = field(default_factory=threading.Lock)
    finished: threading.Event = field(default_factory=threading.Event)


def exit_info_from(state: ContainerState) -> ExitInfo:
    if state.oom_killed or state.exit_code > SIGNAL_EXIT_BASE:
        return ExitInfo(return_code=None, was_killed=True)
    else:
        return ExitInfo(return_code=state.exit_code, was_killed=False)


def read_only_mounts(
    mounts: Mapping[Path, str], refused_roots: tuple[Path, ...]
) -> tuple[BindMount, ...]:
    """Host paths a worker may read, such as a gateway wrapper; never the problem or the run."""
    accepted: list[BindMount] = []
    for host_path, container_path in mounts.items():
        source = host_path.expanduser().resolve()
        if not source.exists():
            raise ValueError(f"mounts_read_only: {host_path} does not exist")
        elif not container_path.startswith("/"):
            raise ValueError(
                f"mounts_read_only: {container_path!r} is not an absolute container path"
            )
        else:
            for root in refused_roots:
                if source.is_relative_to(root.resolve()):
                    raise ValueError(
                        f"mounts_read_only: {host_path} is under {root}, which a "
                        "worker must not see"
                    )
                else:
                    pass
            accepted.append(
                BindMount(source=source, destination=container_path, read_only=True)
            )
    return tuple(accepted)


class DockerExecutor:
    def __init__(
        self,
        *,
        run_id: str,
        image: str,
        sandbox_root: Path,
        log_root: Path,
        blackboard: Blackboard,
        objects: ObjectStore,
        claims: ClaimLedger,
        params: Params,
        clock: Clock,
        docker: DockerClient,
        resources: ContainerResources | None = None,
        mounts_read_only: Mapping[Path, str] | None = None,
        refused_mount_roots: tuple[Path, ...] = (),
        allow_unenforced: bool = False,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        clone_refresh_seconds: float = DEFAULT_CLONE_REFRESH_SECONDS,
        poll_seconds: float | None = DEFAULT_POLL_SECONDS,
    ) -> None:
        """poll_seconds None runs no sidecar thread; the caller then drives tick itself."""
        self.run_id = run_id
        self.image = image
        self.sandbox_root = sandbox_root
        self.log_root = log_root
        self.blackboard = blackboard
        self.objects = objects
        self.claims = claims
        self.params = params
        self.clock = clock
        self.docker = docker
        self.resources = resources or ContainerResources()
        self.mounts_read_only = read_only_mounts(
            mounts_read_only or {}, refused_mount_roots
        )
        self.allow_unenforced = allow_unenforced
        self.grace_seconds = grace_seconds
        self.clone_refresh_seconds = clone_refresh_seconds
        self.egress = RunEgress(
            run_id=run_id,
            image=image,
            log_root=log_root,
            docker=docker,
            allow_unenforced=allow_unenforced,
        )
        self.sessions: dict[str, ContainerSession] = {}
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
                name="docker-executor-sidecar",
                daemon=True,
            )
            self.sidecar.start()

    def sandbox_path(self, agent: ActorId, task_id: str) -> Path:
        """Where a task's sandbox lives on the host; the container sees it as /work."""
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
                handle_id=f"docker-{self.started_count:05d}",
                agent=spec.agent,
                task_id=task.id,
            )
        clone_blackboard(paths, spec.blackboard_clone_url)
        container = self.launch(
            handle, spec, paths, {**spec.files, **start_pack.files}, attempt=1
        )
        now_seconds = self.clock.now_seconds()
        session = ContainerSession(
            handle=handle,
            spec=spec,
            task=task,
            start_pack=start_pack,
            heartbeat_sink=heartbeat_sink,
            paths=paths,
            base_commit=base_commit,
            container=container,
            next_heartbeat_seconds=now_seconds,
            next_refresh_seconds=now_seconds + self.clone_refresh_seconds,
        )
        # The first beat goes out before the sidecar can see the session, so it is sent once.
        with session.lock:
            self.send_heartbeat(session, now_seconds)
        with self.lock:
            self.sessions[handle.handle_id] = session
        return handle

    def environment(
        self, spec: AgentSpec, handle: Handle, attempt: int
    ) -> dict[str, str]:
        """The session's variables, the herd's with container paths, HOME and the proxy."""
        forbidden = sorted(FORBIDDEN_VARIABLES & spec.env.keys())
        if forbidden:
            raise ValueError(f"a worker environment must not contain {forbidden} (D68)")
        else:
            pass
        if self.allow_unenforced:
            logger.warning(
                f"EGRESS NOT ENFORCED for {spec.agent}: its network has a route out, so "
                "only the proxy variables limit it, and a process that ignores them can "
                "reach any host (development only)"
            )
        else:
            pass
        address = self.egress.listener(spec.agent, spec.egress or EgressPolicy())
        proxy_url = f"http://{address.host}:{address.port}"
        return {
            **spec.env,
            **herd_variables(
                spec.agent,
                handle.task_id,
                CONTAINER_PATHS,
                CONTAINER_PATHS.transcript(handle.handle_id, attempt),
            ),
            "HOME": HOME_DIRECTORY,
            **{name: proxy_url for name in PROXY_VARIABLES},
            **{name: "" for name in NO_PROXY_VARIABLES},
        }

    def launch(
        self,
        handle: Handle,
        spec: AgentSpec,
        paths: SandboxPaths,
        files: dict[str, str],
        attempt: int,
    ) -> AgentContainer:
        write_start_files(paths.root, files)
        # The bind mount hides the image's home directory, so the sandbox holds it.
        (paths.root / HOME_NAME).mkdir(exist_ok=True)
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
        name = f"{self.egress.network_name}-{docker_name(handle.handle_id)}-{attempt}"
        container_id = self.docker.run_container(
            ContainerSpec(
                name=name,
                image=self.image,
                argv=spec.command,
                environment=self.environment(spec, handle, attempt),
                labels={
                    RUN_LABEL: self.run_id,
                    AGENT_LABEL: handle.agent,
                    TASK_LABEL: handle.task_id,
                    ATTEMPT_LABEL: str(attempt),
                },
                network=self.egress.network_name,
                mounts=(
                    BindMount(
                        source=paths.root, destination=WORK_DIRECTORY, read_only=False
                    ),
                    *self.mounts_read_only,
                ),
                working_directory=WORK_DIRECTORY,
                cpus=self.resources.cpus,
                memory=self.resources.memory,
            )
        )
        return AgentContainer(
            container_id=container_id,
            name=name,
            attempt=attempt,
            logs=SessionLogs(
                stdout=log_directory / "stdout.log",
                stderr=log_directory / "stderr.log",
                transcript=transcript,
            ),
        )

    def session(self, handle: Handle) -> ContainerSession:
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
                logger.exception("the Docker executor sidecar failed; it keeps running")

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

    def service(self, session: ContainerSession, now_seconds: float) -> None:
        """With session.lock held: notice an exit, or send what is due."""
        if session.finished.is_set():
            return
        else:
            exit_info = self.reap(session)
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

    def send_heartbeat(self, session: ContainerSession, now_seconds: float) -> None:
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

    def refresh(self, session: ContainerSession, now_seconds: float) -> None:
        try:
            refresh_blackboard(session.paths, session.spec.blackboard_clone_url)
        except SIDECAR_FAILURES:
            logger.exception(f"blackboard refresh failed for {session.handle.agent}")
        session.next_refresh_seconds = now_seconds + self.clone_refresh_seconds

    def reap(self, session: ContainerSession) -> ExitInfo | None:
        """The container's exit, once; its logs are kept and the container removed."""
        container = session.container
        if container.exit_info is not None:
            return container.exit_info
        else:
            try:
                state = self.docker.inspect_container(container.container_id)
            except DockerCommandError:
                logger.exception(f"container {container.name} cannot be inspected")
                container.exit_info = ExitInfo(return_code=None, was_killed=True)
                return container.exit_info
            if state.is_running:
                return None
            else:
                container.exit_info = exit_info_from(state)
                self.collect(container)
                return container.exit_info

    def collect(self, container: AgentContainer) -> None:
        try:
            stdout, stderr = self.docker.container_logs(container.container_id)
            container.logs.stdout.write_bytes(stdout)
            container.logs.stderr.write_bytes(stderr)
        except (DockerCommandError, OSError):
            logger.exception(f"could not keep the logs of {container.name}")
        try:
            self.docker.remove_container(container.container_id)
        except DockerCommandError:
            logger.exception(f"could not remove {container.name}; close() will")

    def wait_for_exit(self, session: ContainerSession, seconds: float) -> ExitInfo:
        """After a stop or kill, Docker reports the exit within moments; poll for it."""
        for _ in range(max(1, math.ceil(seconds / EXIT_POLL_SECONDS))):
            exit_info = self.reap(session)
            if exit_info is not None:
                return exit_info
            else:
                time.sleep(EXIT_POLL_SECONDS)
        try:
            self.docker.kill_container(session.container.container_id)
        except DockerCommandError:
            pass
        exit_info = self.reap(session)
        if exit_info is None:
            session.container.exit_info = ExitInfo(return_code=None, was_killed=True)
            return session.container.exit_info
        else:
            return exit_info

    def after_exit(self, session: ContainerSession, exit_info: ExitInfo) -> None:
        """Archive the trace; then restart once, or finish with a stub when there is no claim."""
        handle = session.handle
        has_claim = bool(self.claims.claims_written(handle.agent, handle.task_id))
        if session.stop_reason is not None:
            reason, should_restart = session.stop_reason, False
        elif exit_info.was_killed:
            reason, should_restart = "killed from outside the herd", False
        elif has_claim:
            reason, should_restart = "finished", False
        elif session.container.attempt == 1:
            reason, should_restart = "no claim yet; restarting once with a hint", True
        else:
            reason, should_restart = "no claim after the restart", False
        ended = EndedSession(
            agent=handle.agent,
            task=session.task,
            handle_id=handle.handle_id,
            attempt=session.container.attempt,
            exit_info=exit_info,
            reason=reason,
            logs=session.container.logs,
        )
        restarted = self.restart_with_hint(session) if should_restart else None
        # Book the outcome before the trace, so a failing trace write can neither
        # leave a finished session running nor start the restart a second time.
        if restarted is not None:
            session.ended_containers.append(session.container)
            session.container = restarted
        else:
            session.exit_info = exit_info
        try:
            write_trace(
                self.blackboard,
                self.objects,
                ended,
                needs_stub=not has_claim and restarted is None,
            )
        except Exception:
            logger.exception(
                f"could not archive the trace of {handle.task_id} for {handle.agent}"
            )
        finally:
            if restarted is None:
                session.finished.set()
            else:
                pass

    def restart_with_hint(self, session: ContainerSession) -> AgentContainer | None:
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
        """docker stop with the grace period (SIGTERM, then SIGKILL); then start again."""
        session = self.session(handle)
        with session.lock:
            if not session.finished.is_set():
                session.stop_reason = f"stopped by interrupt {interrupt.id}"
                try:
                    self.docker.stop_container(
                        session.container.container_id, self.grace_seconds
                    )
                except DockerCommandError:
                    logger.exception(f"docker stop failed for {session.container.name}")
                self.after_exit(
                    session, self.wait_for_exit(session, STOP_SETTLE_SECONDS)
                )
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
                try:
                    self.docker.kill_container(session.container.container_id)
                except DockerCommandError:
                    logger.info(f"{session.container.name} had already stopped")
                self.after_exit(
                    session, self.wait_for_exit(session, STOP_SETTLE_SECONDS)
                )

    def wait(self, handle: Handle, timeout_seconds: float | None) -> ExitInfo | None:
        """Wait in real seconds; this executor's own polls notice the exit."""
        session = self.session(handle)
        if timeout_seconds is None:
            step_count = None
        else:
            step_count = max(1, math.ceil(timeout_seconds / EXIT_POLL_SECONDS))
        steps_taken = 0
        while step_count is None or steps_taken < step_count:
            if not self.is_alive(handle):
                return session.exit_info
            else:
                session.finished.wait(EXIT_POLL_SECONDS)
                steps_taken += 1
        if self.is_alive(handle):
            return None
        else:
            return session.exit_info

    def checkpoint(self, handle: Handle) -> bytes:
        raise NotSupportedError("container checkpoints wait for the H11 spike (#24)")

    def restore(self, checkpoint_blob: bytes) -> Handle:
        raise NotSupportedError("container checkpoints wait for the H11 spike (#24)")

    def container_id(self, handle: Handle) -> str:
        """The session's current container."""
        return self.session(handle).container.container_id

    def base_revision(self, agent: ActorId, task_id: str) -> str:
        """target/ is a detached clone; its starting commit is kept at BASE_REF."""
        return BASE_REF

    def transcript_path(self, handle: Handle) -> Path:
        """The host path of the transcript the current container writes at /work/.vlah."""
        return self.session(handle).container.logs.transcript

    def denied(self) -> tuple[DeniedConnection, ...]:
        """Every connection the run's proxy refused, from its per-agent logs."""
        return self.egress.denied()

    def close(self) -> None:
        """Stop the sidecar, kill every session, then remove everything with the run's label."""
        self.stopping.set()
        if self.sidecar is not None:
            self.sidecar.join()
        else:
            pass
        with self.lock:
            handles = [session.handle for session in self.sessions.values()]
        for handle in handles:
            self.kill(handle)
        self.egress.close()
