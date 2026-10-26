"""What the Kubernetes executor asks of a cluster, over kubectl (D59, #131).

The executor speaks to the cluster through this protocol only, so a fake stands in
for it in the unit tests and kubectl does the work in a run. Objects are applied as
manifests: a Job per worker session, and the run's namespace, network policies,
proxy Deployment and Service.
"""

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Protocol

import yaml
from pydantic import JsonValue

KUBECTL_TIMEOUT_SECONDS = 120.0
DNS_LABEL_LIMIT = 63
UNSAFE_NAME_CHARACTERS = re.compile(r"[^a-z0-9-]")
WORKER_USER_ID = 1001
OOM_KILLED = "OOMKilled"


class KubernetesCommandError(RuntimeError):
    pass


def kubernetes_name(text: str) -> str:
    """A run or handle id as a Kubernetes object name allows it: a DNS label."""
    name = UNSAFE_NAME_CHARACTERS.sub("-", text.lower()).strip("-")
    return name[:DNS_LABEL_LIMIT].rstrip("-") or "x"


@dataclass(frozen=True, kw_only=True)
class HostPathVolume:
    """A directory of the node mounted into the pod; the executor scopes it to one task."""

    name: str
    host_path: str
    mount_path: str
    read_only: bool


@dataclass(frozen=True, kw_only=True)
class HostAlias:
    ip: str
    hostname: str


@dataclass(frozen=True, kw_only=True)
class JobSpec:
    """Everything the Job manifest of one worker session is built from."""

    name: str
    namespace: str
    image: str
    argv: tuple[str, ...]
    environment: dict[str, str]
    labels: dict[str, str]
    volumes: tuple[HostPathVolume, ...]
    working_directory: str
    cpus: float | None = None
    memory: str | None = None
    host_aliases: tuple[HostAlias, ...] = ()


@dataclass(frozen=True, kw_only=True)
class PodState:
    """A Job's pod as far as the executor reads it; no pod yet counts as running."""

    is_running: bool
    exit_code: int
    oom_killed: bool
    pod_name: str | None


def resource_limits(cpus: float | None, memory: str | None) -> dict[str, JsonValue]:
    limits: dict[str, JsonValue] = {}
    if cpus is not None:
        limits["cpu"] = str(cpus)
    else:
        pass
    if memory is not None:
        limits["memory"] = memory.upper().replace("G", "Gi").replace("M", "Mi")
    else:
        pass
    return limits


def job_manifest(spec: JobSpec) -> dict[str, JsonValue]:
    """One pod, never restarted by Kubernetes: the executor decides on restarts (D77)."""
    limits = resource_limits(spec.cpus, spec.memory)
    container: dict[str, JsonValue] = {
        "name": "worker",
        "image": spec.image,
        "imagePullPolicy": "IfNotPresent",
        "command": list(spec.argv),
        "workingDir": spec.working_directory,
        "env": [
            {"name": name, "value": value} for name, value in spec.environment.items()
        ],
        "volumeMounts": [
            {
                "name": volume.name,
                "mountPath": volume.mount_path,
                "readOnly": volume.read_only,
            }
            for volume in spec.volumes
        ],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "runAsNonRoot": True,
            "runAsUser": WORKER_USER_ID,
        },
    }
    if limits:
        container["resources"] = {"limits": limits, "requests": limits}
    else:
        pass
    pod_spec: dict[str, JsonValue] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "containers": [container],
        "volumes": [
            {
                "name": volume.name,
                "hostPath": {"path": volume.host_path, "type": "Directory"},
            }
            for volume in spec.volumes
        ],
    }
    if spec.host_aliases:
        pod_spec["hostAliases"] = [
            {"ip": alias.ip, "hostnames": [alias.hostname]}
            for alias in spec.host_aliases
        ]
    else:
        pass
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": spec.name,
            "namespace": spec.namespace,
            "labels": dict(spec.labels),
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": None,
            "template": {
                "metadata": {"labels": dict(spec.labels)},
                "spec": pod_spec,
            },
        },
    }


class KubernetesClient(Protocol):
    def apply(self, manifest: dict[str, JsonValue]) -> None:
        """Create or update one object from its manifest."""
        ...

    def create_job(self, spec: JobSpec) -> None: ...

    def pod_state(self, namespace: str, job_name: str) -> PodState:
        """The Job's newest pod, or a running state before it exists."""
        ...

    def pod_logs(self, namespace: str, pod_name: str) -> bytes:
        """Both streams, as Kubernetes keeps them: one log."""
        ...

    def delete_job(self, namespace: str, job_name: str, grace_seconds: float) -> None:
        """Delete the Job and its pod, giving the pod the grace period to stop."""
        ...

    def jobs_with_label(self, namespace: str, label: str) -> tuple[str, ...]: ...

    def pods_ready(self, namespace: str, label: str) -> bool:
        """Whether every pod with the label is running and ready."""
        ...

    def namespace_exists(self, namespace: str) -> bool: ...

    def delete_namespace(self, namespace: str) -> None: ...

    def node_addresses(self) -> tuple[str, ...]:
        """The nodes' internal IPs: where a host-networked pod is reached."""
        ...


def parse_pod_state(raw: object) -> PodState:
    """The newest pod of a kubectl get pods -o json listing."""
    if not isinstance(raw, dict):
        raise KubernetesCommandError(f"unexpected pod listing: {type(raw).__name__}")
    else:
        pass
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        return PodState(is_running=True, exit_code=0, oom_killed=False, pod_name=None)
    else:
        pass
    newest = sorted(
        (item for item in items if isinstance(item, dict)),
        key=lambda item: str(
            item.get("metadata", {}).get("creationTimestamp", "")
            if isinstance(item.get("metadata"), dict)
            else ""
        ),
    )[-1]
    metadata = newest.get("metadata")
    status = newest.get("status")
    pod_name = str(metadata.get("name")) if isinstance(metadata, dict) else None
    if not isinstance(status, dict):
        return PodState(
            is_running=True, exit_code=0, oom_killed=False, pod_name=pod_name
        )
    else:
        pass
    phase = status.get("phase")
    statuses = status.get("containerStatuses")
    terminated: object = None
    if isinstance(statuses, list) and statuses and isinstance(statuses[0], dict):
        state = statuses[0].get("state")
        terminated = state.get("terminated") if isinstance(state, dict) else None
    else:
        pass
    if isinstance(terminated, dict):
        exit_code = terminated.get("exitCode")
        return PodState(
            is_running=False,
            exit_code=int(exit_code) if isinstance(exit_code, int) else 1,
            oom_killed=terminated.get("reason") == OOM_KILLED,
            pod_name=pod_name,
        )
    elif phase in ("Succeeded", "Failed"):
        return PodState(
            is_running=False,
            exit_code=0 if phase == "Succeeded" else 1,
            oom_killed=False,
            pod_name=pod_name,
        )
    else:
        return PodState(
            is_running=True, exit_code=0, oom_killed=False, pod_name=pod_name
        )


def parse_ready(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    else:
        items = raw.get("items")
        if not isinstance(items, list) or not items:
            return False
        else:
            pass
        for item in items:
            status = item.get("status") if isinstance(item, dict) else None
            conditions = status.get("conditions") if isinstance(status, dict) else None
            is_ready = isinstance(conditions, list) and any(
                isinstance(condition, dict)
                and condition.get("type") == "Ready"
                and condition.get("status") == "True"
                for condition in conditions
            )
            if not is_ready:
                return False
            else:
                continue
        return True


@dataclass(kw_only=True)
class KubectlCli:
    """KubernetesClient over the kubectl command, against one context."""

    context: str | None = None
    executable: str = field(
        default_factory=lambda: shutil.which("kubectl") or "kubectl"
    )
    timeout_seconds: float = KUBECTL_TIMEOUT_SECONDS

    def kubectl(
        self, *arguments: str, stdin_text: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        context = ("--context", self.context) if self.context else ()
        completed = subprocess.run(
            [self.executable, *context, *arguments],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if check and completed.returncode != 0:
            raise KubernetesCommandError(
                f"kubectl {arguments[0]} failed: {completed.stderr.strip()[-2000:]}"
            )
        else:
            return completed

    def unavailable_reason(self) -> str | None:
        if shutil.which(self.executable) is None:
            return "kubectl is not installed"
        else:
            completed = self.kubectl("cluster-info", check=False)
            if completed.returncode == 0:
                return None
            else:
                return f"no cluster answers: {completed.stderr.strip()[-200:]}"

    def apply(self, manifest: dict[str, JsonValue]) -> None:
        self.kubectl("apply", "-f", "-", stdin_text=yaml.safe_dump(manifest))

    def create_job(self, spec: JobSpec) -> None:
        self.apply(job_manifest(spec))

    def pod_state(self, namespace: str, job_name: str) -> PodState:
        completed = self.kubectl(
            "get", "pods", "-n", namespace, "-l", f"job-name={job_name}", "-o", "json"
        )
        return parse_pod_state(json.loads(completed.stdout))

    def pod_logs(self, namespace: str, pod_name: str) -> bytes:
        completed = self.kubectl("logs", "-n", namespace, pod_name, check=False)
        return completed.stdout.encode("utf-8")

    def delete_job(self, namespace: str, job_name: str, grace_seconds: float) -> None:
        self.kubectl(
            "delete",
            "job",
            "-n",
            namespace,
            job_name,
            f"--grace-period={max(0, round(grace_seconds))}",
            "--ignore-not-found",
            "--wait=false",
        )

    def jobs_with_label(self, namespace: str, label: str) -> tuple[str, ...]:
        completed = self.kubectl(
            "get",
            "jobs",
            "-n",
            namespace,
            "-l",
            label,
            "-o",
            "jsonpath={.items[*].metadata.name}",
            check=False,
        )
        return tuple(completed.stdout.split()) if completed.returncode == 0 else ()

    def pods_ready(self, namespace: str, label: str) -> bool:
        completed = self.kubectl(
            "get", "pods", "-n", namespace, "-l", label, "-o", "json", check=False
        )
        if completed.returncode != 0:
            return False
        else:
            return parse_ready(json.loads(completed.stdout))

    def namespace_exists(self, namespace: str) -> bool:
        return self.kubectl("get", "namespace", namespace, check=False).returncode == 0

    def node_addresses(self) -> tuple[str, ...]:
        completed = self.kubectl(
            "get",
            "nodes",
            "-o",
            'jsonpath={.items[*].status.addresses[?(@.type=="InternalIP")].address}',
        )
        return tuple(completed.stdout.split())

    def delete_namespace(self, namespace: str) -> None:
        self.kubectl(
            "delete", "namespace", namespace, "--ignore-not-found", "--wait=false"
        )
