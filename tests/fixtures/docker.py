"""The Docker executor on a real git blackboard, over a fake Docker or the daemon.

FakeDockerClient records every container and network the executor asks for and
lets a test end a container with an exit code; it plays the egress proxy program's
one visible part, answering a listener request with its ready file, when the
executor inspects the proxy container. The infra tests pass a DockerCli instead.
"""

import http.server
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from tests.fixtures.executor import (
    TASK,
    WORKER,
    RecordingClaims,
    make_target_repository,
)
from vlah.adapters.executor.docker.client import (
    ContainerSpec,
    ContainerState,
    DockerCli,
    DockerClient,
    DockerCommandError,
)
from vlah.adapters.executor.docker.egress import (
    IMAGE_PYTHON,
    LOGS_MOUNT,
    POLICIES_DIRECTORY,
    RUN_LABEL,
)
from vlah.adapters.executor.docker.executor import ContainerResources, DockerExecutor
from vlah.adapters.executor.sandbox.container_egress import (
    MODULE,
    ListenerRequest,
    ready_path,
    request_path,
)
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
SIGTERM_EXIT_CODE = 143
SIGKILL_EXIT_CODE = 137
SLEEP_PROGRAM = "import time; time.sleep(600)"
QUICK_PROGRAM = "pass"


@dataclass(kw_only=True)
class FakeContainer:
    container_id: str
    spec: ContainerSpec
    is_running: bool = True
    exit_code: int = 0
    oom_killed: bool = False
    stdout: bytes = b""
    stderr: bytes = b""
    networks: list[str] = field(default_factory=list[str])


@dataclass(kw_only=True)
class FakeNetwork:
    network_id: str
    name: str
    labels: dict[str, str]
    internal: bool


@dataclass(kw_only=True)
class FakeDockerClient:
    containers: dict[str, FakeContainer] = field(
        default_factory=dict[str, FakeContainer]
    )
    networks: dict[str, FakeNetwork] = field(default_factory=dict[str, FakeNetwork])
    stops: list[tuple[str, float]] = field(default_factory=list[tuple[str, float]])
    kills: list[str] = field(default_factory=list[str])
    removed: list[str] = field(default_factory=list[str])
    execute_reply: tuple[int, str] = (0, "")
    created_count: int = 0

    def next_id(self, prefix: str) -> str:
        self.created_count += 1
        return f"{prefix}-{self.created_count:04d}"

    def create_network(
        self, name: str, labels: Mapping[str, str], internal: bool
    ) -> str:
        network_id = self.next_id("net")
        self.networks[network_id] = FakeNetwork(
            network_id=network_id, name=name, labels=dict(labels), internal=internal
        )
        return network_id

    def connect_network(self, network: str, container: str) -> None:
        self.containers[container].networks.append(network)

    def remove_network(self, network: str) -> None:
        self.networks.pop(network)

    def run_container(self, spec: ContainerSpec) -> str:
        container_id = self.next_id("ctr")
        self.containers[container_id] = FakeContainer(
            container_id=container_id, spec=spec, networks=[spec.network]
        )
        return container_id

    def inspect_container(self, container: str) -> ContainerState:
        if container not in self.containers:
            raise DockerCommandError(f"no such container: {container}")
        else:
            found = self.containers[container]
            if found.is_running and MODULE in found.spec.argv:
                self.answer_listener_requests(found)
            else:
                pass
            return ContainerState(
                is_running=found.is_running,
                exit_code=found.exit_code,
                oom_killed=found.oom_killed,
            )

    def answer_listener_requests(self, proxy: FakeContainer) -> None:
        """What the proxy program does with a request file: it writes the ready file."""
        (logs_mount,) = [
            mount for mount in proxy.spec.mounts if mount.destination == LOGS_MOUNT
        ]
        policies = logs_mount.source / POLICIES_DIRECTORY
        for path in policies.glob("*.json"):
            request = ListenerRequest.model_validate_json(path.read_text())
            ready_path(policies, request.agent).write_text(f"{request.generation}\n")

    def stop_container(self, container: str, grace_seconds: float) -> None:
        self.stops.append((container, grace_seconds))
        self.exit(container, SIGTERM_EXIT_CODE)

    def kill_container(self, container: str) -> None:
        self.kills.append(container)
        self.exit(container, SIGKILL_EXIT_CODE)

    def remove_container(self, container: str) -> None:
        self.containers.pop(container)
        self.removed.append(container)

    def container_logs(self, container: str) -> tuple[bytes, bytes]:
        found = self.containers[container]
        return found.stdout, found.stderr

    def execute(self, container: str, argv: tuple[str, ...]) -> tuple[int, str]:
        return self.execute_reply

    def containers_with_label(self, label: str) -> tuple[str, ...]:
        key, _, value = label.partition("=")
        return tuple(
            container_id
            for container_id, found in self.containers.items()
            if found.spec.labels.get(key) == value
        )

    def networks_with_label(self, label: str) -> tuple[str, ...]:
        key, _, value = label.partition("=")
        return tuple(
            network_id
            for network_id, network in self.networks.items()
            if network.labels.get(key) == value
        )

    def exit(self, container: str, exit_code: int, oom_killed: bool = False) -> None:
        """End a running container as Docker would report it."""
        found = self.containers[container]
        found.is_running = False
        found.exit_code = exit_code
        found.oom_killed = oom_killed

    def workers(self) -> list[FakeContainer]:
        """Every container that is not the egress proxy, oldest first."""
        return [
            found for found in self.containers.values() if MODULE not in found.spec.argv
        ]

    def proxy(self) -> FakeContainer:
        (proxy,) = [
            found for found in self.containers.values() if MODULE in found.spec.argv
        ]
        return proxy

    def add_orphan(self, name: str, run_id: str) -> str:
        """A container a crashed control plane left behind, found only by its label."""
        return self.run_container(
            ContainerSpec(
                name=name,
                image=WORKER_IMAGE,
                argv=("sleep",),
                environment={},
                labels={RUN_LABEL: run_id},
                network="bridge",
                mounts=(),
                working_directory="/",
            )
        )


@dataclass(kw_only=True)
class DockerExecutorHarness:
    executor: DockerExecutor
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
            command=(IMAGE_PYTHON, "-c", program),
            env=env or {},
            target_repository=str(self.target),
            target_ref="main",
            blackboard_clone_url=self.clone_url,
            egress=egress,
        )

    def sandbox(self, agent: ActorId = WORKER, task_id: str = TASK.id) -> Path:
        return self.executor.sandbox_path(agent, task_id)

    def listener_request(self, agent: ActorId = WORKER) -> ListenerRequest:
        return ListenerRequest.model_validate_json(
            request_path(self.log_root / POLICIES_DIRECTORY, agent).read_text()
        )


@contextmanager
def running_docker_executor(
    tmp_path: Path,
    clock: Clock,
    *,
    docker: DockerClient | None = None,
    image: str = WORKER_IMAGE,
    params: Params | None = None,
    poll_seconds: float | None = None,
    grace_seconds: float = 0.5,
    sandbox_root: Path | None = None,
    objects: MemoryObjectStore | None = None,
    resources: ContainerResources | None = None,
    mounts_read_only: Mapping[Path, str] | None = None,
    refused_mount_roots: tuple[Path, ...] = (),
    allow_unenforced: bool = False,
    run_id: str = RUN_ID,
    close_on_exit: bool = True,
) -> Iterator[DockerExecutorHarness]:
    """close_on_exit False simulates a crashed control plane: nothing is cleaned up."""
    blackboard = open_blackboard(tmp_path / "runs", run_id, "blackboard")
    blackboard.write(ActorId("human"), {"spec.md": b"the spec"}, "spec")
    objects = objects or MemoryObjectStore()
    claims = RecordingClaims()
    log_root = tmp_path / "logs"
    executor = DockerExecutor(
        run_id=run_id,
        image=image,
        sandbox_root=sandbox_root or tmp_path / "sandboxes",
        log_root=log_root,
        blackboard=blackboard,
        objects=objects,
        claims=claims,
        params=params or Params(),
        clock=clock,
        docker=docker if docker is not None else FakeDockerClient(),
        resources=resources,
        mounts_read_only=mounts_read_only,
        refused_mount_roots=refused_mount_roots,
        allow_unenforced=allow_unenforced,
        grace_seconds=grace_seconds,
        poll_seconds=poll_seconds,
    )
    try:
        yield DockerExecutorHarness(
            executor=executor,
            blackboard=blackboard,
            objects=objects,
            claims=claims,
            sink=RecordingHeartbeatSink(),
            target=make_target_repository(tmp_path / "target"),
            clone_url=blackboard.clone_url(),
            log_root=log_root,
        )
    finally:
        if close_on_exit:
            executor.close()
        else:
            pass


class HelloHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, ContainerFacingServer)
        server.paths.append(self.path)
        body = b"hello from the host"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *arguments: object) -> None:
        pass


class ContainerFacingServer(http.server.ThreadingHTTPServer):
    """A stand-in for the tool server that containers reach as host.docker.internal."""

    def __init__(self) -> None:
        super().__init__(("0.0.0.0", 0), HelloHandler)
        self.paths: list[str] = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


def image_missing_reason(image: str = WORKER_IMAGE) -> str | None:
    """Why a test that needs the worker image must skip, or None when it can run."""
    cli = DockerCli()
    reason = cli.unavailable_reason()
    if reason is not None:
        return reason
    elif not cli.image_exists(image):
        return f"{image} is not built: docker build -t {image} -f deploy/worker/Dockerfile ."
    else:
        return None
