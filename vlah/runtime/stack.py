"""The herd's services on this machine, up, down and checked: vlah stack (#129).

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
