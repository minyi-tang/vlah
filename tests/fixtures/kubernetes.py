"""The Kubernetes executor on a real git blackboard, over a fake cluster.

FakeKubernetes records every manifest the executor applies and lets a test end a
pod with an exit code; it plays the egress proxy pod's one visible part, answering
a listener request file with its ready file, when the executor applies the proxy
Deployment or asks whether the pod is ready. The infra tests pass a KubectlCli.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from tests.fixtures.executor import (
    TASK,
    WORKER,
    RecordingClaims,
    make_target_repository,
)
from vlah.adapters.executor.kubernetes.client import (
    JobSpec,
    KubernetesCommandError,
    PodState,
    job_manifest,
)
from vlah.adapters.executor.kubernetes.egress import (
    AGENT_LABEL,
    LOGS_MOUNT,
    POLICIES_DIRECTORY,
    PROXY_AGENT,
)
from vlah.adapters.executor.kubernetes.executor import KubernetesExecutor, PodResources
from vlah.adapters.executor.sandbox.container_egress import ListenerRequest, ready_path
from vlah.adapters.memory.store import MemoryObjectStore
from vlah.core.blackboard.api import Blackboard
from vlah.core.blackboard.layout import open_blackboard
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy, Params
from vlah.ports.executor import AgentSpec
from vlah.testing.conformance import RecordingHeartbeatSink

RUN_ID = "run-1"
WORKER_IMAGE = "vlah-worker:dev"
SLEEP_PROGRAM = "import time; time.sleep(600)"
QUICK_PROGRAM = "pass"


@dataclass(kw_only=True)
class FakeJob:
    spec: JobSpec
    manifest: dict[str, JsonValue]
    is_running: bool = True
    exit_code: int = 0
    oom_killed: bool = False
    log: bytes = b""
    deleted_with_grace: float | None = None


@dataclass(kw_only=True)
class FakeKubernetes:
    """A cluster that keeps what it was told; manifests by (kind, namespace, name)."""

    manifests: dict[tuple[str, str, str], dict[str, JsonValue]] = field(
        default_factory=dict[tuple[str, str, str], dict[str, JsonValue]]
    )
    jobs: dict[tuple[str, str], FakeJob] = field(
        default_factory=dict[tuple[str, str], FakeJob]
    )
    deleted_namespaces: list[str] = field(default_factory=list[str])
    proxy_log_root: Path | None = None

    def apply(self, manifest: dict[str, JsonValue]) -> None:
        kind = str(manifest.get("kind"))
        metadata = manifest.get("metadata")
        assert isinstance(metadata, dict)
        name = str(metadata.get("name"))
        namespace = str(metadata.get("namespace", ""))
        self.manifests[(kind, namespace, name)] = manifest
        if kind == "Deployment":
            self.remember_proxy_mount(manifest)
        else:
            pass

    def remember_proxy_mount(self, manifest: dict[str, JsonValue]) -> None:
        """Where the proxy pod would read requests: the hostPath behind /logs."""
        spec = manifest.get("spec")
        template = spec.get("template") if isinstance(spec, dict) else None
        pod_spec = template.get("spec") if isinstance(template, dict) else None
        volumes = pod_spec.get("volumes") if isinstance(pod_spec, dict) else None
        if isinstance(volumes, list):
            for volume in volumes:
                host_path = volume.get("hostPath") if isinstance(volume, dict) else None
                if isinstance(host_path, dict):
                    self.proxy_log_root = Path(str(host_path.get("path")))
                else:
                    pass
        else:
            pass

    def answer_listener_requests(self) -> None:
        """What the proxy program does with a request file: it writes the ready file."""
        if self.proxy_log_root is None:
            return
        else:
            policies = self.proxy_log_root / POLICIES_DIRECTORY
            for path in policies.glob("*.json"):
                request = ListenerRequest.model_validate_json(path.read_text())
                ready_path(policies, request.agent).write_text(
                    f"{request.generation}\n"
                )

    def create_job(self, spec: JobSpec) -> None:
        manifest = job_manifest(spec)
        self.manifests[("Job", spec.namespace, spec.name)] = manifest
        self.jobs[(spec.namespace, spec.name)] = FakeJob(spec=spec, manifest=manifest)
        self.answer_listener_requests()

    def pod_state(self, namespace: str, job_name: str) -> PodState:
        job = self.jobs.get((namespace, job_name))
        if job is None:
            raise KubernetesCommandError(f"no such job: {job_name}")
        else:
            self.answer_listener_requests()
            return PodState(
                is_running=job.is_running,
                exit_code=job.exit_code,
                oom_killed=job.oom_killed,
                pod_name=f"{job_name}-pod",
            )

    def pod_logs(self, namespace: str, pod_name: str) -> bytes:
        for (job_namespace, job_name), job in self.jobs.items():
            if job_namespace == namespace and f"{job_name}-pod" == pod_name:
                return job.log
            else:
                continue
        return b""

    def delete_job(self, namespace: str, job_name: str, grace_seconds: float) -> None:
        job = self.jobs.get((namespace, job_name))
        if job is not None:
            # The first delete is the one that stopped it; collect's cleanup follows.
            if job.deleted_with_grace is None:
                job.deleted_with_grace = grace_seconds
            else:
                pass
            if job.is_running:
                job.is_running = False
                job.exit_code = 143 if grace_seconds > 0 else 137
            else:
                pass
        else:
            pass

    def jobs_with_label(self, namespace: str, label: str) -> tuple[str, ...]:
        key, _, value = label.partition("=")
        return tuple(
            name
            for (job_namespace, name), job in self.jobs.items()
            if job_namespace == namespace and job.spec.labels.get(key) == value
        )

    def pods_ready(self, namespace: str, label: str) -> bool:
        self.answer_listener_requests()
        return any(
            key[0] == "Deployment" and key[1] == namespace for key in self.manifests
        )

    def namespace_exists(self, namespace: str) -> bool:
        return ("Namespace", "", namespace) in self.manifests

    def node_addresses(self) -> tuple[str, ...]:
        return ("172.18.0.2",)

    def delete_namespace(self, namespace: str) -> None:
        self.deleted_namespaces.append(namespace)
        self.manifests = {
            key: manifest
            for key, manifest in self.manifests.items()
            if key[1] != namespace
            and not (key[0] == "Namespace" and key[2] == namespace)
        }
        self.jobs = {key: job for key, job in self.jobs.items() if key[0] != namespace}

    def exit(self, job_name: str, exit_code: int, oom_killed: bool = False) -> None:
        for job in self.jobs.values():
            if job.spec.name == job_name:
                job.is_running = False
                job.exit_code = exit_code
                job.oom_killed = oom_killed
            else:
                continue

    def worker_jobs(self) -> list[FakeJob]:
        return [
            job
            for job in self.jobs.values()
            if job.spec.labels.get(AGENT_LABEL) != PROXY_AGENT
        ]

    def proxy_deployment(self) -> dict[str, JsonValue]:
        (manifest,) = [
            manifest
            for key, manifest in self.manifests.items()
            if key[0] == "Deployment"
        ]
        return manifest

    def add_orphan_namespace(self, namespace: str) -> None:
        """A namespace a crashed control plane left behind."""
        self.manifests[("Namespace", "", namespace)] = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace},
        }


@dataclass(kw_only=True)
class KubernetesExecutorHarness:
    executor: KubernetesExecutor
    kube: FakeKubernetes
    blackboard: Blackboard
    objects: MemoryObjectStore
    claims: RecordingClaims
    sink: RecordingHeartbeatSink
    target: Path
    clone_url: str
    log_root: Path

    def spec(
        self,
        program: str,
        agent: ActorId = WORKER,
        env: dict[str, str] | None = None,
        egress: EgressPolicy | None = None,
    ) -> AgentSpec:
        return AgentSpec(
            agent=agent,
            command=("python", "-c", program),
            env=env or {},
            target_repository=str(self.target),
            target_ref="main",
            blackboard_clone_url=self.clone_url,
            egress=egress,
        )

    def sandbox(self, agent: ActorId = WORKER, task_id: str = TASK.id) -> Path:
        return self.executor.sandbox_path(agent, task_id)

    def listener_request(self, agent: ActorId = WORKER) -> ListenerRequest:
        path = self.log_root / POLICIES_DIRECTORY / f"{agent}.json"
        return ListenerRequest.model_validate_json(path.read_text())


@contextmanager
def running_kubernetes_executor(
    tmp_path: Path,
    clock: Clock,
    *,
    kube: FakeKubernetes | None = None,
    image: str = WORKER_IMAGE,
    params: Params | None = None,
    poll_seconds: float | None = None,
    grace_seconds: float = 0.5,
    resources: PodResources | None = None,
    allow_unenforced: bool = False,
    run_id: str = RUN_ID,
) -> Iterator[KubernetesExecutorHarness]:
    blackboard = open_blackboard(tmp_path / "runs", run_id, "blackboard")
    blackboard.write(ActorId("human"), {"spec.md": b"the spec"}, "spec")
    objects = MemoryObjectStore()
    claims = RecordingClaims()
    log_root = tmp_path / "logs"
    kube = kube if kube is not None else FakeKubernetes()
    executor = KubernetesExecutor(
        run_id=run_id,
        image=image,
        sandbox_root=tmp_path / "sandboxes",
        log_root=log_root,
        blackboard=blackboard,
        objects=objects,
        claims=claims,
        params=params or Params(),
        clock=clock,
        kube=kube,
        resources=resources,
        # The fake cluster sees every directory; the real one mounts the home.
        node_mount=tmp_path,
        allow_unenforced=allow_unenforced,
        grace_seconds=grace_seconds,
        poll_seconds=poll_seconds,
    )
    try:
        yield KubernetesExecutorHarness(
            executor=executor,
            kube=kube,
            blackboard=blackboard,
            objects=objects,
            claims=claims,
            sink=RecordingHeartbeatSink(),
            target=make_target_repository(tmp_path / "target"),
            clone_url=blackboard.clone_url(),
            log_root=log_root,
        )
    finally:
        executor.close()


__all__ = [
    "LOGS_MOUNT",
    "FakeKubernetes",
    "KubernetesExecutorHarness",
    "running_kubernetes_executor",
]
