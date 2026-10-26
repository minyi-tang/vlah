"""The herd's services on this machine, up, down and checked: vlah stack (#129, #131).

The stack is one Docker Compose project, vlah: Redis with append-only persistence
on a named volume, the store a run on the stack keeps its queue, leases and budget
in. Workers' containers and networks belong to a run, labelled vlah.run=<run id>
by the Docker executor; down removes what a crashed run left behind. check is the
pre-flight: one line per item, and any failure makes the exit code 1.

Every command runs through RunCommand, so the tests script Docker's answers.
"""

import re
import subprocess
from dataclasses import dataclass
from typing import Protocol, TextIO

import yaml
from pydantic import JsonValue

COMPOSE_PROJECT = "vlah"
REDIS_SERVICE = "redis"
RUN_LABEL = "vlah.run"
DEFAULT_WORKER_IMAGE = "vlah-worker:dev"
# How containers reach this machine; Linux needs the host-gateway alias for it.
CONTAINER_HOST = "host.docker.internal"
HOST_GATEWAY_FLAG = f"--add-host={CONTAINER_HOST}:host-gateway"
COMMAND_TIMEOUT_SECONDS = 300.0
PROBE_TIMEOUT_SECONDS = 10
MEMORY_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
MEMORY_TEXT = re.compile(r"^(?P<number>\d+(\.\d+)?)(?P<unit>[bkmg]?)$")
COMPOSE_DEFINITION: dict[str, JsonValue] = {
    "name": COMPOSE_PROJECT,
    "services": {
        REDIS_SERVICE: {
            "image": "redis:7-alpine",
            "command": ["redis-server", "--appendonly", "yes"],
            "ports": ["127.0.0.1:6379:6379"],
            "volumes": ["vlah-redis:/data"],
            "healthcheck": {
                "test": ["CMD", "redis-cli", "ping"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 20,
            },
        }
    },
    "volumes": {"vlah-redis": {}},
}
KIND_CLUSTER = "vlah"
KIND_CONTEXT = f"kind-{KIND_CLUSTER}"
KIND_NODE = f"{KIND_CLUSTER}-control-plane"
# Pinned and bumped on purpose; kindnet does not enforce NetworkPolicy, Calico does.
CALICO_MANIFEST_URL = "https://raw.githubusercontent.com/projectcalico/calico/v3.29.1/manifests/calico.yaml"
CALICO_POD_SUBNET = "192.168.0.0/16"
KIND_WAIT = "180s"
HOST_PROBE = (
    "import socket, sys, urllib.error, urllib.request\n"
    "address = socket.gethostbyname(sys.argv[1])\n"
    "print('resolved', address)\n"
    "if len(sys.argv) > 2:\n"
    "    try:\n"
    f"        urllib.request.urlopen(sys.argv[2], timeout={PROBE_TIMEOUT_SECONDS}).read()\n"
    "    except urllib.error.HTTPError:\n"
    "        pass\n"
    "    print('reached')\n"
    "else:\n"
    "    pass\n"
)
GATEWAY_PROBE = (
    "import sys, urllib.error, urllib.request\n"
    "try:\n"
    f"    urllib.request.urlopen(sys.argv[1], timeout={PROBE_TIMEOUT_SECONDS}).read()\n"
    "except urllib.error.HTTPError:\n"
    "    pass\n"
    "print('reached')\n"
)


@dataclass(frozen=True, kw_only=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @property
    def detail(self) -> str:
        """The last line the command printed, for one-line reports."""
        lines = (self.stdout.strip() or self.stderr.strip()).splitlines()
        return lines[-1] if lines else ""


class RunCommand(Protocol):
    def __call__(
        self, argv: tuple[str, ...], stdin_text: str | None = None
    ) -> CommandResult:
        """Run a command to completion; never raises on a non-zero exit."""
        ...


def run_command(argv: tuple[str, ...], stdin_text: str | None = None) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as error:
        return CommandResult(exit_code=127, stdout="", stderr=str(error))
    except subprocess.TimeoutExpired as error:
        return CommandResult(exit_code=124, stdout="", stderr=str(error))
    return CommandResult(
        exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )


def compose_yaml() -> str:
    return yaml.safe_dump(COMPOSE_DEFINITION, sort_keys=False)


def compose(run: RunCommand, *arguments: str) -> CommandResult:
    return run(
        ("docker", "compose", "-p", COMPOSE_PROJECT, "-f", "-", *arguments),
        stdin_text=compose_yaml(),
    )


def memory_bytes(text: str) -> int:
    """A Docker memory size such as 2g or 512m, in bytes."""
    match = MEMORY_TEXT.fullmatch(text.strip().lower())
    if match is None:
        raise ValueError(f"memory must look like 2g or 512m, not {text!r}")
    else:
        return int(
            float(match.group("number")) * MEMORY_UNITS[match.group("unit") or "b"]
        )


def stack_up(run: RunCommand, stream: TextIO) -> int:
    result = compose(run, "up", "-d", "--wait")
    if result.passed:
        print(f"stack {COMPOSE_PROJECT} is up: {REDIS_SERVICE}", file=stream)
        return 0
    else:
        print(f"stack {COMPOSE_PROJECT} did not come up: {result.detail}", file=stream)
        return 1


def labelled_ids(run: RunCommand, kind: str, run_id: str | None) -> list[str]:
    label_filter = f"label={RUN_LABEL}" + (f"={run_id}" if run_id else "")
    if kind == "container":
        argv = ("docker", "ps", "-aq", "--filter", label_filter)
    else:
        argv = ("docker", "network", "ls", "-q", "--filter", label_filter)
    return run(argv).stdout.split()


def stack_down(
    run: RunCommand, stream: TextIO, run_id: str | None, remove_volumes: bool
) -> int:
    """Stop the services (unless one run is named) and remove what runs left behind."""
    exit_code = 0
    if run_id is None:
        result = compose(run, "down", *(("--volumes",) if remove_volumes else ()))
        if result.passed:
            print(f"stack {COMPOSE_PROJECT} is down", file=stream)
        else:
            print(f"stack {COMPOSE_PROJECT} did not stop: {result.detail}", file=stream)
            exit_code = 1
    else:
        pass
    containers = labelled_ids(run, "container", run_id)
    if containers:
        run(("docker", "rm", "-f", *containers))
    else:
        pass
    networks = labelled_ids(run, "network", run_id)
    if networks:
        run(("docker", "network", "rm", *networks))
    else:
        pass
    scope = f"run {run_id}" if run_id else "every run"
    print(
        f"removed {len(containers)} container(s) and {len(networks)} network(s) of {scope}",
        file=stream,
    )
    return exit_code


@dataclass(frozen=True, kw_only=True)
class Check:
    name: str
    passed: bool
    detail: str


def check_stack(
    run: RunCommand,
    *,
    image: str = DEFAULT_WORKER_IMAGE,
    gateway_url: str | None = None,
    worker_count: int = 1,
    memory_text: str = "2g",
) -> list[Check]:
    """The pre-flight, in the order a failure is most likely to explain the next."""
    checks: list[Check] = []
    docker_info = run(
        ("docker", "info", "--format", "{{.ServerVersion}} {{.MemTotal}}")
    )
    checks.append(
        Check(
            name="docker",
            passed=docker_info.passed,
            detail=(
                f"server {docker_info.stdout.split()[0]}"
                if docker_info.passed and docker_info.stdout.split()
                else docker_info.detail
            ),
        )
    )
    if not docker_info.passed:
        return checks
    else:
        pass
    compose_version = run(("docker", "compose", "version", "--short"))
    checks.append(
        Check(
            name="compose", passed=compose_version.passed, detail=compose_version.detail
        )
    )
    image_inspect = run(("docker", "image", "inspect", "--format", "{{.Id}}", image))
    checks.append(
        Check(
            name="worker image",
            passed=image_inspect.passed,
            detail=(
                image
                if image_inspect.passed
                else f"{image} is missing; build it with docker build -t {image} "
                "-f deploy/worker/Dockerfile ."
            ),
        )
    )
    ping = compose(run, "exec", "-T", REDIS_SERVICE, "redis-cli", "ping")
    checks.append(
        Check(
            name="redis",
            passed=ping.passed and "PONG" in ping.stdout,
            detail=(
                "PONG" if "PONG" in ping.stdout else f"{ping.detail}; run vlah stack up"
            ),
        )
    )
    if image_inspect.passed:
        host_name = run(
            (
                "docker",
                "run",
                "--rm",
                HOST_GATEWAY_FLAG,
                image,
                "getent",
                "hosts",
                CONTAINER_HOST,
            )
        )
        checks.append(
            Check(
                name="host name",
                passed=host_name.passed,
                detail=(
                    f"{CONTAINER_HOST} resolves in a container"
                    if host_name.passed
                    else f"{CONTAINER_HOST} does not resolve in a container: {host_name.detail}"
                ),
            )
        )
        if gateway_url is not None:
            gateway = run(
                (
                    "docker",
                    "run",
                    "--rm",
                    HOST_GATEWAY_FLAG,
                    image,
                    "python",
                    "-c",
                    GATEWAY_PROBE,
                    gateway_url,
                )
            )
            checks.append(
                Check(
                    name="model gateway",
                    passed=gateway.passed and "reached" in gateway.stdout,
                    detail=(
                        f"{gateway_url} answers from a container"
                        if gateway.passed
                        else f"{gateway_url} is unreachable from a container: {gateway.detail}"
                    ),
                )
            )
        else:
            pass
    else:
        pass
    memory_total = (
        int(docker_info.stdout.split()[1]) if len(docker_info.stdout.split()) > 1 else 0
    )
    wanted = worker_count * memory_bytes(memory_text)
    checks.append(
        Check(
            name="memory",
            passed=wanted <= memory_total,
            detail=(
                f"{worker_count} worker(s) x {memory_text} = {wanted / 1024**3:.1f} GiB "
                f"of {memory_total / 1024**3:.1f} GiB available to Docker"
            ),
        )
    )
    return checks


def print_checks(checks: list[Check], stream: TextIO) -> int:
    for check in checks:
        state = "ok  " if check.passed else "FAIL"
        print(f"{state} {check.name}: {check.detail}", file=stream)
    failed = [check for check in checks if not check.passed]
    if failed:
        print(f"{len(failed)} check(s) failed", file=stream)
        return 1
    else:
        print("the stack is ready", file=stream)
        return 0


def kind_cluster_yaml(home: str) -> str:
    """The kind cluster: no default CNI (Calico follows), the home directory mounted
    into the node so a worker pod can mount its own sandbox and nothing else (#131)."""
    definition: dict[str, JsonValue] = {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "name": KIND_CLUSTER,
        "networking": {"disableDefaultCNI": True, "podSubnet": CALICO_POD_SUBNET},
        "nodes": [
            {
                "role": "control-plane",
                "extraMounts": [
                    {"hostPath": home, "containerPath": home, "readOnly": False}
                ],
            }
        ],
    }
    return yaml.safe_dump(definition, sort_keys=False)


def kubectl(run: RunCommand, *arguments: str) -> CommandResult:
    return run(("kubectl", "--context", KIND_CONTEXT, *arguments))


def kind_up(run: RunCommand, stream: TextIO, home: str, image: str) -> int:
    """Create the cluster if it is missing, install Calico, wait, load the worker image."""
    clusters = run(("kind", "get", "clusters"))
    if KIND_CLUSTER in clusters.stdout.split():
        print(f"kind cluster {KIND_CLUSTER} exists", file=stream)
    else:
        created = run(
            ("kind", "create", "cluster", "--config", "-", "--wait", KIND_WAIT),
            stdin_text=kind_cluster_yaml(home),
        )
        if not created.passed:
            print(f"kind create cluster failed: {created.detail}", file=stream)
            return 1
        else:
            print(f"kind cluster {KIND_CLUSTER} created", file=stream)
    calico = kubectl(run, "apply", "-f", CALICO_MANIFEST_URL)
    if not calico.passed:
        print(f"Calico did not apply: {calico.detail}", file=stream)
        return 1
    else:
        pass
    ready = kubectl(
        run,
        "wait",
        "--for=condition=Ready",
        "nodes",
        "--all",
        f"--timeout={KIND_WAIT}",
    )
    if not ready.passed:
        print(f"the node did not become Ready: {ready.detail}", file=stream)
        return 1
    else:
        pass
    calico_ready = kubectl(
        run,
        "wait",
        "--for=condition=Ready",
        "pods",
        "-n",
        "kube-system",
        "-l",
        "k8s-app=calico-node",
        f"--timeout={KIND_WAIT}",
    )
    if not calico_ready.passed:
        print(f"Calico did not become Ready: {calico_ready.detail}", file=stream)
        return 1
    else:
        pass
    loaded = run(("kind", "load", "docker-image", image, "--name", KIND_CLUSTER))
    if not loaded.passed:
        print(f"could not load {image} into the cluster: {loaded.detail}", file=stream)
        return 1
    else:
        print(
            f"kind cluster {KIND_CLUSTER} is ready with Calico and {image}", file=stream
        )
        return 0


def kind_down(run: RunCommand, stream: TextIO) -> int:
    deleted = run(("kind", "delete", "cluster", "--name", KIND_CLUSTER))
    if deleted.passed:
        print(f"kind cluster {KIND_CLUSTER} is gone", file=stream)
        return 0
    else:
        print(f"kind delete cluster failed: {deleted.detail}", file=stream)
        return 1


def check_kind(
    run: RunCommand,
    *,
    image: str = DEFAULT_WORKER_IMAGE,
    gateway_url: str | None = None,
) -> list[Check]:
    """The pre-flight for the Kubernetes executor on kind."""
    checks: list[Check] = []
    for tool in ("kind", "kubectl"):
        version = run((tool, "version"))
        checks.append(Check(name=tool, passed=version.passed, detail=version.detail))
    if not all(check.passed for check in checks):
        return checks
    else:
        pass
    cluster = kubectl(run, "cluster-info")
    checks.append(
        Check(
            name="cluster",
            passed=cluster.passed,
            detail=(
                f"{KIND_CONTEXT} answers"
                if cluster.passed
                else f"{cluster.detail}; run vlah stack --kind up"
            ),
        )
    )
    if not cluster.passed:
        return checks
    else:
        pass
    calico = kubectl(
        run,
        "get",
        "pods",
        "-n",
        "kube-system",
        "-l",
        "k8s-app=calico-node",
        "-o",
        "jsonpath={.items[*].status.phase}",
    )
    checks.append(
        Check(
            name="calico",
            passed=calico.passed and "Running" in calico.stdout,
            detail=(
                "calico-node is Running (NetworkPolicy is enforced)"
                if "Running" in calico.stdout
                else "calico-node is not running; NetworkPolicy would not be enforced"
            ),
        )
    )
    on_node = run(("docker", "exec", KIND_NODE, "crictl", "images", "-q", image))
    checks.append(
        Check(
            name="worker image on the node",
            passed=on_node.passed and bool(on_node.stdout.strip()),
            detail=(
                f"{image} is on {KIND_NODE}"
                if on_node.stdout.strip()
                else f"{image} is not on the node; run vlah stack --kind up"
            ),
        )
    )
    probe_arguments = [CONTAINER_HOST] + ([gateway_url] if gateway_url else [])
    probe = kubectl(
        run,
        "run",
        "vlah-stack-probe",
        "--rm",
        "-i",
        "--restart=Never",
        f"--image={image}",
        "--image-pull-policy=IfNotPresent",
        # The proxy pod runs on the node's network; the probe asks what it will see.
        '--overrides={"spec": {"hostNetwork": true}}',
        "--",
        "python",
        "-c",
        HOST_PROBE,
        *probe_arguments,
    )
    checks.append(
        Check(
            name="host from the node",
            # The probe exits non-zero when the gateway fails; the name still resolved.
            passed="resolved" in probe.stdout,
            detail=(
                f"{CONTAINER_HOST} resolves on the node"
                + (" and the gateway answers" if "reached" in probe.stdout else "")
                if "resolved" in probe.stdout
                else f"{CONTAINER_HOST} does not resolve on the node: {probe.detail}"
            ),
        )
    )
    if gateway_url is not None and "resolved" in probe.stdout:
        checks.append(
            Check(
                name="model gateway from the node",
                passed="reached" in probe.stdout,
                detail=(
                    f"{gateway_url} answers from the node"
                    if "reached" in probe.stdout
                    else f"{gateway_url} is unreachable from the node: {probe.detail}"
                ),
            )
        )
    else:
        pass
    return checks
