"""The Kubernetes executor: one Job per worker session, the pod is the sandbox (D32, D60, #131).

The shape is the Docker executor's (#128) with Kubernetes objects: a session is a
Job in the run's namespace whose pod runs the agent's command from the worker
image, mounts only its task's sandbox at /work, and reaches nothing but the run's
egress proxy. The sandbox stays on this machine, under the problem's .herd, and
reaches the pod through the kind node's mount as a hostPath scoped to that one
directory; so the sidecar's heartbeat (git diff on the sandbox), the clone refresh
and the trace archiving are the local executor's code unchanged. A real cluster
would give the sandbox a volume; that is the Service and PVC work of a later issue.

Kubernetes never restarts the pod (backoffLimit 0, restartPolicy Never): the
executor decides, as D77 says, whether a session that left no claim runs once more.
"""

import logging
import math
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from vlah.adapters.executor.kubernetes.client import (
    HostAlias,
    HostPathVolume,
    JobSpec,
    KubernetesClient,
    KubernetesCommandError,
    PodState,
    kubernetes_name,
)
from vlah.adapters.executor.kubernetes.egress import (
    AGENT_LABEL,
    ATTEMPT_LABEL,
    RUN_LABEL,
    TASK_LABEL,
    NamespaceEgress,
    run_label,
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
    ViewSettings,
    check_out_target,
    clone_blackboard,
    files_touched,
    herd_variables,
    refresh_blackboard,
    refuse_sandbox_inside_run_directory,
    sandbox_paths,
    view_environment,
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
SANDBOX_VOLUME = "sandbox"
DEFAULT_CPUS = 2.0
DEFAULT_MEMORY = "2g"
DEFAULT_GRACE_SECONDS = 30.0
DEFAULT_CLONE_REFRESH_SECONDS = 60.0
DEFAULT_POLL_SECONDS = 1.0
# How often wait and the stop paths ask the cluster about a pod.
EXIT_POLL_SECONDS = 0.5
STOP_SETTLE_SECONDS = 30.0
# A signal exit is 128 plus the signal number, as shells report it.
SIGNAL_EXIT_BASE = 128
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
NO_PROXY_VARIABLES = ("NO_PROXY", "no_proxy")
SIDECAR_FAILURES = (GitCommandError, subprocess.TimeoutExpired, OSError)


@dataclass(frozen=True, kw_only=True)
class PodResources:
    """Limits every worker pod gets, in Docker's units so herd.yaml reads the same."""

    cpus: float = DEFAULT_CPUS
    memory: str = DEFAULT_MEMORY


@dataclass(kw_only=True)
class AgentJob:
    name: str
    attempt: int
    logs: SessionLogs
    pod_name: str | None = None
    exit_info: ExitInfo | None = None


@dataclass(kw_only=True)
class JobSession:
    """One task's session: its current Job, the ones before it, and what is due."""

    handle: Handle
    spec: AgentSpec
    task: Task
    start_pack: StartPack
    heartbeat_sink: HeartbeatSink
    paths: SandboxPaths
    base_commit: str
    job: AgentJob
    next_heartbeat_seconds: float
    next_refresh_seconds: float
    last_files_touched: tuple[str, ...] = ()
    stop_reason: str | None = None
    exit_info: ExitInfo | None = None
    ended_jobs: list[AgentJob] = field(default_factory=list[AgentJob])
    lock: threading.Lock = field(default_factory=threading.Lock)
    finished: threading.Event = field(default_factory=threading.Event)


def exit_info_from(state: PodState) -> ExitInfo:
    if state.oom_killed or state.exit_code > SIGNAL_EXIT_BASE:
        return ExitInfo(return_code=None, was_killed=True)
    else:
        return ExitInfo(return_code=state.exit_code, was_killed=False)


class KubernetesExecutor:
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
        kube: KubernetesClient,
        resources: PodResources | None = None,
        host_aliases: tuple[HostAlias, ...] = (),
        allow_unenforced: bool = False,
        view_settings: ViewSettings | None = None,
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
        self.kube = kube
        self.resources = resources or PodResources()
        self.host_aliases = host_aliases
        self.allow_unenforced = allow_unenforced
        self.view_settings = view_settings
        self.grace_seconds = grace_seconds
        self.clone_refresh_seconds = clone_refresh_seconds
        self.egress = NamespaceEgress(
            run_id=run_id,
            image=image,
            log_root=log_root,
            kube=kube,
            host_aliases=host_aliases,
            allow_unenforced=allow_unenforced,
        )
        self.remove_leftovers()
        self.sessions: dict[str, JobSession] = {}
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
                name="kubernetes-executor-sidecar",
                daemon=True,
            )
            self.sidecar.start()

    @property
    def namespace(self) -> str:
        return self.egress.namespace

    def sandbox_path(self, agent: ActorId, task_id: str) -> Path:
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
                handle_id=f"kube-{self.started_count:05d}",
                agent=spec.agent,
                task_id=task.id,
            )
        clone_blackboard(
            paths, spec.blackboard_clone_url, view_environment(spec, self.view_settings)
        )
        job = self.launch(
            handle, spec, paths, {**spec.files, **start_pack.files}, attempt=1
        )
        now_seconds = self.clock.now_seconds()
        session = JobSession(
            handle=handle,
            spec=spec,
            task=task,
            start_pack=start_pack,
            heartbeat_sink=heartbeat_sink,
            paths=paths,
            base_commit=base_commit,
            job=job,
            next_heartbeat_seconds=now_seconds,
            next_refresh_seconds=now_seconds + self.clone_refresh_seconds,
        )
        with session.lock:
            self.send_heartbeat(session, now_seconds)
        with self.lock:
            self.sessions[handle.handle_id] = session
        return handle

    def environment(
        self, spec: AgentSpec, handle: Handle, attempt: int
    ) -> dict[str, str]:
        """The session's variables, the herd's with pod paths, HOME and the proxy."""
        forbidden = sorted(FORBIDDEN_VARIABLES & spec.env.keys())
        if forbidden:
            raise ValueError(f"a worker environment must not contain {forbidden} (D68)")
        else:
            pass
        if self.allow_unenforced:
            logger.warning(
                f"EGRESS NOT ENFORCED for {spec.agent}: the namespace has no deny "
                "policy, so only the proxy variables limit it (development only)"
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
    ) -> AgentJob:
        write_start_files(paths.root, files)
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
        name = kubernetes_name(f"w-{handle.handle_id}-{attempt}")
        self.kube.create_job(
            JobSpec(
                name=name,
                namespace=self.namespace,
                image=self.image,
                argv=spec.command,
                environment=self.environment(spec, handle, attempt),
                labels={
                    RUN_LABEL: self.run_id,
                    AGENT_LABEL: handle.agent,
                    TASK_LABEL: handle.task_id,
                    ATTEMPT_LABEL: str(attempt),
                },
                volumes=(
                    HostPathVolume(
                        name=SANDBOX_VOLUME,
                        host_path=str(paths.root.resolve()),
                        mount_path=WORK_DIRECTORY,
                        read_only=False,
                    ),
                ),
                working_directory=WORK_DIRECTORY,
                cpus=self.resources.cpus,
                memory=self.resources.memory,
                host_aliases=self.host_aliases,
            )
        )
        return AgentJob(
            name=name,
            attempt=attempt,
            logs=SessionLogs(
                stdout=log_directory / "stdout.log",
                stderr=log_directory / "stderr.log",
                transcript=transcript,
            ),
        )

    def session(self, handle: Handle) -> JobSession:
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
                logger.exception(
                    "the Kubernetes executor sidecar failed; it keeps running"
                )

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

    def service(self, session: JobSession, now_seconds: float) -> None:
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

    def send_heartbeat(self, session: JobSession, now_seconds: float) -> None:
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

    def refresh(self, session: JobSession, now_seconds: float) -> None:
        try:
            refresh_blackboard(
                session.paths,
                session.spec.blackboard_clone_url,
                view_environment(session.spec, self.view_settings),
            )
        except SIDECAR_FAILURES:
            logger.exception(f"blackboard refresh failed for {session.handle.agent}")
        session.next_refresh_seconds = now_seconds + self.clone_refresh_seconds

    def reap(self, session: JobSession) -> ExitInfo | None:
        """The pod's exit, once; its log is kept and the Job deleted."""
        job = session.job
        if job.exit_info is not None:
            return job.exit_info
        else:
            try:
                state = self.kube.pod_state(self.namespace, job.name)
            except KubernetesCommandError:
                logger.exception(f"job {job.name} cannot be read")
                job.exit_info = ExitInfo(return_code=None, was_killed=True)
                return job.exit_info
            job.pod_name = state.pod_name or job.pod_name
            if state.is_running:
                return None
            else:
                job.exit_info = exit_info_from(state)
                self.collect(job)
                return job.exit_info

    def collect(self, job: AgentJob) -> None:
        try:
            if job.pod_name is not None:
                log = self.kube.pod_logs(self.namespace, job.pod_name)
            else:
                log = b""
            # Kubernetes keeps one stream per container; stderr.log stays empty.
            job.logs.stdout.write_bytes(log)
            job.logs.stderr.write_bytes(b"")
        except (KubernetesCommandError, OSError):
            logger.exception(f"could not keep the log of {job.name}")
        try:
            self.kube.delete_job(self.namespace, job.name, 0.0)
        except KubernetesCommandError:
            logger.exception(f"could not delete {job.name}; close() will")

    def wait_for_exit(self, session: JobSession, seconds: float) -> ExitInfo:
        """After a delete, the pod ends within the grace period; poll for it."""
        for _ in range(max(1, math.ceil(seconds / EXIT_POLL_SECONDS))):
            exit_info = self.reap(session)
            if exit_info is not None:
                return exit_info
            else:
                time.sleep(EXIT_POLL_SECONDS)
        exit_info = self.reap(session)
        if exit_info is None:
            session.job.exit_info = ExitInfo(return_code=None, was_killed=True)
            return session.job.exit_info
        else:
            return exit_info

    def after_exit(self, session: JobSession, exit_info: ExitInfo) -> None:
        """Archive the trace; then restart once, or finish with a stub when there is no claim."""
        handle = session.handle
        has_claim = bool(self.claims.claims_written(handle.agent, handle.task_id))
        if session.stop_reason is not None:
            reason, should_restart = session.stop_reason, False
        elif exit_info.was_killed:
            reason, should_restart = "killed from outside the herd", False
        elif has_claim:
            reason, should_restart = "finished", False
        elif session.job.attempt == 1:
            reason, should_restart = "no claim yet; restarting once with a hint", True
        else:
            reason, should_restart = "no claim after the restart", False
        ended = EndedSession(
            agent=handle.agent,
            task=session.task,
            handle_id=handle.handle_id,
            attempt=session.job.attempt,
            exit_info=exit_info,
            reason=reason,
            logs=session.job.logs,
        )
        restarted = self.restart_with_hint(session) if should_restart else None
        if restarted is not None:
            session.ended_jobs.append(session.job)
            session.job = restarted
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

    def restart_with_hint(self, session: JobSession) -> AgentJob | None:
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

    def stop_job(self, session: JobSession, reason: str, grace_seconds: float) -> None:
        """With session.lock held: delete the Job and book the exit."""
        session.stop_reason = reason
        try:
            self.kube.delete_job(self.namespace, session.job.name, grace_seconds)
        except KubernetesCommandError:
            logger.exception(f"kubectl delete failed for {session.job.name}")
        self.after_exit(session, self.wait_for_exit(session, STOP_SETTLE_SECONDS))

    def interrupt(self, handle: Handle, interrupt: Interrupt) -> Handle:
        """Delete the Job with the grace period (SIGTERM, then SIGKILL); then start again."""
        session = self.session(handle)
        with session.lock:
            if not session.finished.is_set():
                self.stop_job(
                    session, f"stopped by interrupt {interrupt.id}", self.grace_seconds
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
                self.stop_job(session, "killed by the herd", 0.0)

    def wait(self, handle: Handle, timeout_seconds: float | None) -> ExitInfo | None:
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
        raise NotSupportedError("pod checkpoints wait for the H11 spike (#24)")

    def restore(self, checkpoint_blob: bytes) -> Handle:
        raise NotSupportedError("pod checkpoints wait for the H11 spike (#24)")

    def job_name(self, handle: Handle) -> str:
        return self.session(handle).job.name

    def base_revision(self, agent: ActorId, task_id: str) -> str:
        return BASE_REF

    def transcript_path(self, handle: Handle) -> Path:
        return self.session(handle).job.logs.transcript

    def denied(self) -> tuple[DeniedConnection, ...]:
        return self.egress.denied()

    def remove_leftovers(self) -> None:
        """Delete the run's namespace if an earlier process left it (#130): this
        executor started none of its Jobs and holds no session for them."""
        if self.kube.namespace_exists(self.namespace):
            logger.warning(
                f"deleting namespace {self.namespace}, left by an earlier process"
            )
            self.kube.delete_namespace(self.namespace)
            self.wait_for_namespace_gone()
        else:
            pass

    def wait_for_namespace_gone(self) -> None:
        deadline = time.monotonic() + STOP_SETTLE_SECONDS
        while time.monotonic() < deadline and self.kube.namespace_exists(
            self.namespace
        ):
            time.sleep(EXIT_POLL_SECONDS)

    def close(self) -> None:
        """Stop the sidecar, kill every session, then delete the namespace."""
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


def labels_of(mapping: Mapping[str, str], run_id: str) -> bool:
    return mapping.get(RUN_LABEL) == run_id


__all__ = [
    "KubernetesExecutor",
    "PodResources",
    "exit_info_from",
    "labels_of",
    "run_label",
]
