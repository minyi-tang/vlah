"""A run's way out: an internal network and one proxy container, a listener per agent (D75).

The network is created with no route out, so a worker container reaches nothing
but what is on it: the egress proxy container, which also sits on the bridge and
resolves host.docker.internal to the machine. Each agent gets its own port on that
container, so every denied connection is that agent's; the request for it is a
file the proxy program picks up from a directory under the run's log root, which
the container mounts. With allow_unenforced the network is an ordinary bridge
network: the proxy still runs and logs denials, but nothing stops a worker that
ignores the proxy variables (development only).
"""

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from vlah.adapters.executor.docker.client import (
    BindMount,
    ContainerSpec,
    DockerClient,
    DockerCommandError,
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
BRIDGE_NETWORK = "bridge"
# Docker Engine on Linux needs this to reach the machine; Docker Desktop resolves
# the name anyway, so the proxy container always gets it.
HOST_GATEWAY_ALIAS = "host.docker.internal:host-gateway"
IMAGE_PYTHON = "python"
LOGS_MOUNT = "/logs"
POLICIES_DIRECTORY = "egress"
FIRST_PROXY_PORT = 3128
READY_POLL_SECONDS = 0.05
DEFAULT_READY_TIMEOUT_SECONDS = 30.0
UNSAFE_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9_.-]")


def docker_name(text: str) -> str:
    """A run or handle id as a Docker object name allows it."""
    return UNSAFE_NAME_CHARACTERS.sub("-", text)


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


@dataclass(kw_only=True)
class RunEgress:
    """The run's network and proxy container, started on the first listener request."""

    run_id: str
    image: str
    log_root: Path
    docker: DockerClient
    allow_unenforced: bool = False
    ready_timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS
    proxy_container_id: str | None = None
    ports: dict[ActorId, int] = field(default_factory=dict[ActorId, int])
    generations: dict[ActorId, int] = field(default_factory=dict[ActorId, int])
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def network_name(self) -> str:
        return f"vlah-{docker_name(self.run_id)}"

    @property
    def proxy_name(self) -> str:
        return f"{self.network_name}-proxy"

    @property
    def policies_directory(self) -> Path:
        return self.log_root / POLICIES_DIRECTORY

    def ensure_started(self) -> None:
        """With the lock held: the network and the proxy container, once per run."""
        if self.proxy_container_id is not None:
            return
        else:
            self.policies_directory.mkdir(parents=True, exist_ok=True)
            self.docker.create_network(
                self.network_name,
                {RUN_LABEL: self.run_id},
                internal=not self.allow_unenforced,
            )
            # Started on the bridge, so its default route leads out; the run's
            # network is attached second and carries only the workers' traffic.
            self.proxy_container_id = self.docker.run_container(
                ContainerSpec(
                    name=self.proxy_name,
                    image=self.image,
                    argv=(
                        IMAGE_PYTHON,
                        "-m",
                        MODULE,
                        "--policies",
                        f"{LOGS_MOUNT}/{POLICIES_DIRECTORY}",
                        "--denied",
                        LOGS_MOUNT,
                    ),
                    environment={},
                    labels={RUN_LABEL: self.run_id, AGENT_LABEL: PROXY_AGENT},
                    network=BRIDGE_NETWORK,
                    mounts=(
                        BindMount(
                            source=self.log_root,
                            destination=LOGS_MOUNT,
                            read_only=False,
                        ),
                    ),
                    working_directory=LOGS_MOUNT,
                    extra_hosts=(HOST_GATEWAY_ALIAS,),
                )
            )
            self.docker.connect_network(self.network_name, self.proxy_container_id)

    def listener(self, agent: ActorId, policy: EgressPolicy) -> ProxyAddress:
        """Ask the proxy container for the agent's listener and wait until it is up."""
        with self.lock:
            self.ensure_started()
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
            return ProxyAddress(host=self.proxy_name, port=port)

    def wait_until_ready(self, agent: ActorId, generation: int) -> None:
        assert self.proxy_container_id is not None
        deadline = time.monotonic() + self.ready_timeout_seconds
        while time.monotonic() < deadline:
            if read_ready_generation(self.policies_directory, agent) == generation:
                return
            elif not self.docker.inspect_container(self.proxy_container_id).is_running:
                _, stderr = self.docker.container_logs(self.proxy_container_id)
                raise RuntimeError(
                    f"the egress proxy container {self.proxy_name} exited: "
                    f"{stderr.decode(errors='replace')[-2000:]}"
                )
            else:
                time.sleep(READY_POLL_SECONDS)
        raise RuntimeError(
            f"the egress proxy container {self.proxy_name} did not open {agent}'s "
            f"listener within {self.ready_timeout_seconds:g} s"
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
        """Remove every container and network that carries the run's label."""
        label = run_label(self.run_id)
        for container in self.docker.containers_with_label(label):
            try:
                self.docker.remove_container(container)
            except DockerCommandError:
                logger.exception(f"could not remove container {container}")
        for network in self.docker.networks_with_label(label):
            try:
                self.docker.remove_network(network)
            except DockerCommandError:
                logger.exception(f"could not remove network {network}")
        with self.lock:
            self.proxy_container_id = None
            self.ports.clear()
            self.generations.clear()
