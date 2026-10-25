"""A run's way out of the cluster: a namespace that denies egress, one proxy pod (D75, #131).

Worker pods carry the agent label, and the namespace's NetworkPolicy lets them
reach only the cluster's DNS and the proxy pod; the proxy pod, the one pod without
that restriction, runs the allowlist proxy program from the worker image and is
the workers' only way to the tool server, the model API and the declared sources.
Each agent gets its own listener port on the proxy, asked for through the same
request files the Docker executor writes: the proxy pod mounts the run's log root,
which lives on this machine and reaches the kind node through its mount. With
allow_unenforced the policy is left out (development only).
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from vlah.adapters.executor.kubernetes.client import (
    WORKER_USER_ID,
    HostAlias,
    KubernetesClient,
    KubernetesCommandError,
    kubernetes_name,
)
from vlah.adapters.executor.sandbox.api import DeniedConnection
from vlah.adapters.executor.sandbox.container_egress import (
    DENIED_SUFFIX,
    MODULE,
    ListenerRequest,
    read_ready_generation,
    write_request,
)
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy

logger = logging.getLogger(__name__)

RUN_LABEL = "vlah.run"
AGENT_LABEL = "vlah.agent"
TASK_LABEL = "vlah.task"
ATTEMPT_LABEL = "vlah.attempt"
PROXY_AGENT = "egress-proxy"
PROXY_NAME = "egress-proxy"
NAMESPACE_PREFIX = "vlah-"
IMAGE_PYTHON = "python"
LOGS_MOUNT = "/logs"
POLICIES_DIRECTORY = "egress"
FIRST_PROXY_PORT = 3128
# The Service and the policy name the ports up front; a run has at most this many agents.
PROXY_PORT_COUNT = 64
DNS_PORT = 53
READY_POLL_SECONDS = 0.1
POD_CHECK_EVERY_POLLS = 20
DEFAULT_READY_TIMEOUT_SECONDS = 120.0


def namespace_for(run_id: str) -> str:
    return kubernetes_name(f"{NAMESPACE_PREFIX}{run_id}")


def run_label(run_id: str) -> str:
    return f"{RUN_LABEL}={run_id}"


@dataclass(frozen=True, kw_only=True)
class ProxyAddress:
    host: str
    port: int


def parse_denied(path: Path, agent: ActorId) -> list[DeniedConnection]:
    """The proxy's tab-separated lines: timestamp, host, port."""
    denied: list[DeniedConnection] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[2].isdigit():
            denied.append(
                DeniedConnection(
                    timestamp_seconds=float(fields[0]),
                    agent=agent,
                    host=fields[1],
                    port=int(fields[2]),
                )
            )
        else:
            pass
    return denied


def namespace_manifest(namespace: str, run_id: str) -> dict[str, JsonValue]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace, "labels": {RUN_LABEL: run_id}},
    }


def deny_workers_manifest(namespace: str, run_id: str) -> dict[str, JsonValue]:
    """Every pod with an agent label but the proxy reaches only DNS and the proxy."""
    dns_rule: dict[str, JsonValue] = {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                },
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ],
        "ports": [
            {"protocol": "UDP", "port": DNS_PORT},
            {"protocol": "TCP", "port": DNS_PORT},
        ],
    }
    proxy_rule: dict[str, JsonValue] = {
        "to": [{"podSelector": {"matchLabels": {AGENT_LABEL: PROXY_AGENT}}}],
        "ports": [
            {
                "protocol": "TCP",
                "port": FIRST_PROXY_PORT,
                "endPort": FIRST_PROXY_PORT + PROXY_PORT_COUNT - 1,
            }
        ],
    }
    pod_selector: dict[str, JsonValue] = {
        "matchExpressions": [
            {"key": AGENT_LABEL, "operator": "Exists"},
            {"key": AGENT_LABEL, "operator": "NotIn", "values": [PROXY_AGENT]},
        ]
    }
    spec: dict[str, JsonValue] = {
        "podSelector": pod_selector,
        "policyTypes": ["Egress"],
        "egress": [dns_rule, proxy_rule],
    }
    metadata: dict[str, JsonValue] = {
        "name": "deny-workers-egress",
        "namespace": namespace,
        "labels": {RUN_LABEL: run_id},
    }
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": metadata,
        "spec": spec,
    }


def proxy_deployment_manifest(
    namespace: str,
    run_id: str,
    image: str,
    log_root: Path,
    host_aliases: tuple[HostAlias, ...],
) -> dict[str, JsonValue]:
    labels = {RUN_LABEL: run_id, AGENT_LABEL: PROXY_AGENT}
    pod_spec: dict[str, JsonValue] = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "containers": [
            {
                "name": "proxy",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": [
                    IMAGE_PYTHON,
                    "-m",
                    MODULE,
                    "--policies",
                    f"{LOGS_MOUNT}/{POLICIES_DIRECTORY}",
                    "--denied",
                    LOGS_MOUNT,
                ],
                "workingDir": LOGS_MOUNT,
                "volumeMounts": [{"name": "logs", "mountPath": LOGS_MOUNT}],
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "runAsNonRoot": True,
                    "runAsUser": WORKER_USER_ID,
                },
            }
        ],
        "volumes": [
            {
                "name": "logs",
                "hostPath": {"path": str(log_root), "type": "DirectoryOrCreate"},
            }
        ],
    }
    if host_aliases:
        pod_spec["hostAliases"] = [
            {"ip": alias.ip, "hostnames": [alias.hostname]} for alias in host_aliases
        ]
    else:
        pass
    metadata: dict[str, JsonValue] = {
        "name": PROXY_NAME,
        "namespace": namespace,
        "labels": dict(labels),
    }
    template: dict[str, JsonValue] = {
        "metadata": {"labels": dict(labels)},
        "spec": pod_spec,
    }
    spec: dict[str, JsonValue] = {
        "replicas": 1,
        "selector": {"matchLabels": {AGENT_LABEL: PROXY_AGENT}},
        "template": template,
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": metadata,
        "spec": spec,
    }


def proxy_service_manifest(namespace: str, run_id: str) -> dict[str, JsonValue]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": PROXY_NAME,
            "namespace": namespace,
            "labels": {RUN_LABEL: run_id},
        },
        "spec": {
            "selector": {AGENT_LABEL: PROXY_AGENT},
            "ports": [
                {
                    "name": f"p{port}",
                    "port": port,
                    "targetPort": port,
                    "protocol": "TCP",
                }
                for port in range(FIRST_PROXY_PORT, FIRST_PROXY_PORT + PROXY_PORT_COUNT)
            ],
        },
    }


@dataclass(kw_only=True)
class NamespaceEgress:
    """The run's namespace, policy and proxy pod, created on the first listener request."""

    run_id: str
    image: str
    log_root: Path
    kube: KubernetesClient
    host_aliases: tuple[HostAlias, ...] = ()
    allow_unenforced: bool = False
    ready_timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS
    is_started: bool = False
    ports: dict[ActorId, int] = field(default_factory=dict[ActorId, int])
    generations: dict[ActorId, int] = field(default_factory=dict[ActorId, int])
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def namespace(self) -> str:
        return namespace_for(self.run_id)

    @property
    def policies_directory(self) -> Path:
        return self.log_root / POLICIES_DIRECTORY

    def manifests(self) -> list[dict[str, JsonValue]]:
        namespace = self.namespace
        objects = [namespace_manifest(namespace, self.run_id)]
        if not self.allow_unenforced:
            objects.append(deny_workers_manifest(namespace, self.run_id))
        else:
            pass
        objects.append(
            proxy_deployment_manifest(
                namespace, self.run_id, self.image, self.log_root, self.host_aliases
            )
        )
        objects.append(proxy_service_manifest(namespace, self.run_id))
        return objects

    def ensure_started(self) -> None:
        """With the lock held: the namespace and everything in it, once per run."""
        if self.is_started:
            return
        else:
            self.policies_directory.mkdir(parents=True, exist_ok=True)
            for manifest in self.manifests():
                self.kube.apply(manifest)
            self.is_started = True

    def listener(self, agent: ActorId, policy: EgressPolicy) -> ProxyAddress:
        """Ask the proxy pod for the agent's listener and wait until it is up."""
        with self.lock:
            self.ensure_started()
            if agent not in self.ports and len(self.ports) >= PROXY_PORT_COUNT:
                raise RuntimeError(
                    f"the egress proxy serves at most {PROXY_PORT_COUNT} agents per run"
                )
            else:
                pass
            port = self.ports.setdefault(agent, FIRST_PROXY_PORT + len(self.ports))
            generation = self.generations.get(agent, 0) + 1
            self.generations[agent] = generation
            write_request(
                self.policies_directory,
                ListenerRequest(
                    agent=agent, port=port, generation=generation, policy=policy
                ),
            )
            self.wait_until_ready(agent, generation)
            return ProxyAddress(host=PROXY_NAME, port=port)

    def wait_until_ready(self, agent: ActorId, generation: int) -> None:
        """Poll the ready file; ask the cluster about the proxy pod now and then, so a
        pod that never comes up is in the error and not only the timeout."""
        deadline = time.monotonic() + self.ready_timeout_seconds
        ready = False
        polls = 0
        while time.monotonic() < deadline:
            if read_ready_generation(self.policies_directory, agent) == generation:
                return
            elif polls % POD_CHECK_EVERY_POLLS == 0:
                ready = self.kube.pods_ready(
                    self.namespace, f"{AGENT_LABEL}={PROXY_AGENT}"
                )
            else:
                pass
            polls += 1
            time.sleep(READY_POLL_SECONDS)
        raise RuntimeError(
            f"the egress proxy pod in {self.namespace} did not open {agent}'s listener "
            f"within {self.ready_timeout_seconds:g} s (pod ready: {ready})"
        )

    def denied(self) -> tuple[DeniedConnection, ...]:
        denied: list[DeniedConnection] = []
        if self.log_root.is_dir():
            for path in sorted(self.log_root.glob(f"*{DENIED_SUFFIX}")):
                denied.extend(
                    parse_denied(path, ActorId(path.name.removesuffix(DENIED_SUFFIX)))
                )
        else:
            pass
        return tuple(
            sorted(
                denied, key=lambda item: (item.timestamp_seconds, item.agent, item.host)
            )
        )

    def close(self) -> None:
        """Delete the namespace: every Job, pod, policy and the proxy go with it."""
        try:
            self.kube.delete_namespace(self.namespace)
        except KubernetesCommandError:
            logger.exception(f"could not delete namespace {self.namespace}")
        with self.lock:
            self.is_started = False
            self.ports.clear()
            self.generations.clear()
