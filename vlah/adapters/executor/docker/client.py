"""The slice of Docker the executor uses, behind a protocol a test can fake.

DockerCli speaks to the daemon through the docker command, so the herd needs no
extra dependency and the same calls work against Docker Desktop and Docker
Engine. Every call is synchronous; the executor's sidecar polls inspect.
"""

import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

DOCKER_TIMEOUT_SECONDS = 120.0
INFO_TIMEOUT_SECONDS = 30.0


class DockerCommandError(RuntimeError):
    pass


@dataclass(frozen=True, kw_only=True)
class BindMount:
    source: Path
    destination: str
    read_only: bool


@dataclass(frozen=True, kw_only=True)
class ContainerSpec:
    """Everything docker run is told about one container."""

    name: str
    image: str
    argv: tuple[str, ...]
    environment: dict[str, str]
    labels: dict[str, str]
    network: str
    mounts: tuple[BindMount, ...]
    working_directory: str
    cpus: float | None = None
    memory: str | None = None
    extra_hosts: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ContainerState:
    """docker inspect's State, as far as the executor reads it."""

    is_running: bool
    exit_code: int
    oom_killed: bool


class DockerClient(Protocol):
    def create_network(
        self, name: str, labels: Mapping[str, str], internal: bool
    ) -> str:
        """Create a bridge network; internal networks have no route out (D75)."""
        ...

    def connect_network(self, network: str, container: str) -> None: ...

    def remove_network(self, network: str) -> None: ...

    def run_container(self, spec: ContainerSpec) -> str:
        """Start a detached container; return its id."""
        ...

    def inspect_container(self, container: str) -> ContainerState: ...

    def stop_container(self, container: str, grace_seconds: float) -> None:
        """SIGTERM the container's init, then SIGKILL after the grace period."""
        ...

    def kill_container(self, container: str) -> None: ...

    def remove_container(self, container: str) -> None: ...

    def container_logs(self, container: str) -> tuple[bytes, bytes]:
        """stdout and stderr of the container so far."""
        ...

    def execute(self, container: str, argv: tuple[str, ...]) -> tuple[int, str]:
        """Run a command in a running container; return its exit code and output."""
        ...

    def containers_with_label(self, label: str) -> tuple[str, ...]:
        """Ids of every container, running or not, that carries the label."""
        ...

    def networks_with_label(self, label: str) -> tuple[str, ...]: ...


def parse_state(raw: object) -> ContainerState:
    """Narrow docker inspect's decoded State object (an untrusted boundary)."""
    if not isinstance(raw, dict):
        raise DockerCommandError(f"docker inspect returned no State object: {raw!r}")
    else:
        state: dict[str, object] = {str(key): value for key, value in raw.items()}
        running = state.get("Running")
        exit_code = state.get("ExitCode")
        oom_killed = state.get("OOMKilled")
        if (
            not isinstance(running, bool)
            or not isinstance(exit_code, int)
            or not isinstance(oom_killed, bool)
        ):
            raise DockerCommandError(f"docker inspect's State is malformed: {raw!r}")
        else:
            return ContainerState(
                is_running=running, exit_code=exit_code, oom_killed=oom_killed
            )


def run_arguments(spec: ContainerSpec) -> list[str]:
    """The docker run command line for a spec, with --init so signals reach the agent.

    Without an init process the agent's program is pid 1, which ignores a SIGTERM
    it has no handler for, so every interrupt would wait out the whole grace period.
    """
    arguments = [
        "run",
        "--detach",
        "--init",
        "--name",
        spec.name,
        "--network",
        spec.network,
        "--workdir",
        spec.working_directory,
    ]
    for key, value in sorted(spec.labels.items()):
        arguments.extend(("--label", f"{key}={value}"))
    for mount in spec.mounts:
        options = f"type=bind,src={mount.source},dst={mount.destination}"
        arguments.extend(
            ("--mount", options + (",readonly" if mount.read_only else ""))
        )
    for key, value in sorted(spec.environment.items()):
        arguments.extend(("--env", f"{key}={value}"))
    for host in spec.extra_hosts:
        arguments.extend(("--add-host", host))
    if spec.cpus is not None:
        arguments.extend(("--cpus", str(spec.cpus)))
    else:
        pass
    if spec.memory is not None:
        arguments.extend(("--memory", spec.memory))
    else:
        pass
    return [*arguments, spec.image, *spec.argv]


@dataclass(kw_only=True)
class DockerCli:
    """DockerClient over the docker command."""

    executable: str = field(default_factory=lambda: shutil.which("docker") or "docker")
    timeout_seconds: float = DOCKER_TIMEOUT_SECONDS

    def docker(
        self, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [self.executable, *arguments],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if check and completed.returncode != 0:
            raise DockerCommandError(
                f"docker {arguments[0]} failed: {completed.stderr.strip()[-2000:]}"
            )
        else:
            return completed

    def unavailable_reason(self) -> str | None:
        """None when the daemon answers; otherwise why containers cannot run here."""
        if shutil.which(self.executable) is None:
            return "docker is not installed"
        else:
            completed = subprocess.run(
                [self.executable, "info"],
                capture_output=True,
                text=True,
                timeout=INFO_TIMEOUT_SECONDS,
                check=False,
            )
            if completed.returncode == 0:
                return None
            else:
                return f"docker is not running: {completed.stderr.strip()[-200:]}"

    def image_exists(self, image: str) -> bool:
        return self.docker("image", "inspect", image, check=False).returncode == 0

    def create_network(
        self, name: str, labels: Mapping[str, str], internal: bool
    ) -> str:
        arguments = ["network", "create", "--driver", "bridge"]
        for key, value in sorted(labels.items()):
            arguments.extend(("--label", f"{key}={value}"))
        if internal:
            arguments.append("--internal")
        else:
            pass
        return self.docker(*arguments, name).stdout.strip()

    def connect_network(self, network: str, container: str) -> None:
        self.docker("network", "connect", network, container)

    def remove_network(self, network: str) -> None:
        self.docker("network", "rm", network)

    def run_container(self, spec: ContainerSpec) -> str:
        return self.docker(*run_arguments(spec)).stdout.strip()

    def inspect_container(self, container: str) -> ContainerState:
        raw = self.docker(
            "inspect", "--type", "container", "--format", "{{json .State}}", container
        ).stdout
        return parse_state(json.loads(raw))

    def stop_container(self, container: str, grace_seconds: float) -> None:
        self.docker("stop", "--time", str(max(0, round(grace_seconds))), container)

    def kill_container(self, container: str) -> None:
        self.docker("kill", container)

    def remove_container(self, container: str) -> None:
        self.docker("rm", "--force", "--volumes", container)

    def container_logs(self, container: str) -> tuple[bytes, bytes]:
        completed = subprocess.run(
            [self.executable, "logs", container],
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise DockerCommandError(
                f"docker logs failed: {completed.stderr.decode(errors='replace')[-2000:]}"
            )
        else:
            return completed.stdout, completed.stderr

    def execute(self, container: str, argv: tuple[str, ...]) -> tuple[int, str]:
        completed = self.docker("exec", container, *argv, check=False)
        return completed.returncode, completed.stdout + completed.stderr

    def containers_with_label(self, label: str) -> tuple[str, ...]:
        listing = self.docker(
            "ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={label}"
        ).stdout
        return tuple(listing.split())

    def networks_with_label(self, label: str) -> tuple[str, ...]:
        listing = self.docker(
            "network", "ls", "--quiet", "--no-trunc", "--filter", f"label={label}"
        ).stdout
        return tuple(listing.split())
