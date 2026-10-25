"""vlah stack: the pre-flight names what fails, down removes a run's leftovers, the
compose file in deploy/ is the one the command sends (#129)."""

import io
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from vlah.runtime.stack import (
    COMPOSE_PROJECT,
    CommandResult,
    check_kind,
    check_stack,
    compose_yaml,
    kind_up,
    memory_bytes,
    print_checks,
    stack_down,
    stack_up,
)

pytestmark = pytest.mark.runtime

REPO_ROOT = Path(__file__).resolve().parents[3]
GIBIBYTE = 1024**3


@dataclass(kw_only=True)
class ScriptedDocker:
    """Answers each command from a table keyed by its first distinctive words."""

    answers: dict[str, CommandResult]
    memory_total: int = 8 * GIBIBYTE
    calls: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])
    stdin_texts: list[str | None] = field(default_factory=list[str | None])

    def __call__(
        self, argv: tuple[str, ...], stdin_text: str | None = None
    ) -> CommandResult:
        self.calls.append(argv)
        self.stdin_texts.append(stdin_text)
        if argv[:2] == ("docker", "info"):
            return CommandResult(
                exit_code=0, stdout=f"29.2.0 {self.memory_total}\n", stderr=""
            )
        else:
            pass
        for key, answer in self.answers.items():
            if key in " ".join(argv):
                return answer
            else:
                continue
        return CommandResult(exit_code=0, stdout="", stderr="")


OK = CommandResult(exit_code=0, stdout="", stderr="")
PONG = CommandResult(exit_code=0, stdout="PONG\n", stderr="")
MISSING = CommandResult(exit_code=1, stdout="", stderr="Error: No such image")
REFUSED = CommandResult(exit_code=1, stdout="", stderr="connection refused")


def test_check_passes_a_ready_stack_and_names_each_failure() -> None:
    ready = ScriptedDocker(
        answers={
            "redis-cli ping": PONG,
            "getent hosts": OK,
            "python -c": CommandResult(exit_code=0, stdout="reached\n", stderr=""),
        }
    )
    checks = check_stack(
        ready, gateway_url="http://host.docker.internal:4000", worker_count=2
    )
    assert [check.name for check in checks] == [
        "docker",
        "compose",
        "worker image",
        "redis",
        "host name",
        "model gateway",
        "memory",
    ]
    assert all(check.passed for check in checks), [check.detail for check in checks]
    stream = io.StringIO()
    assert print_checks(checks, stream) == 0
    assert "the stack is ready" in stream.getvalue()
    assert any(
        "--add-host=host.docker.internal:host-gateway" in call for call in ready.calls
    ), "the Linux host alias is always passed"

    broken = ScriptedDocker(
        answers={"image inspect": MISSING, "redis-cli ping": REFUSED},
        memory_total=3 * GIBIBYTE,
    )
    checks = check_stack(broken, worker_count=2)
    failed = {check.name: check.detail for check in checks if not check.passed}
    assert set(failed) == {"worker image", "redis", "memory"}
    assert "docker build" in failed["worker image"]
    assert "vlah stack up" in failed["redis"]
    assert "4.0 GiB of 3.0 GiB" in failed["memory"]
    assert "host name" not in {check.name for check in checks}, "no image, no probe"
    stream = io.StringIO()
    assert print_checks(checks, stream) == 1
    assert "FAIL worker image" in stream.getvalue()


def test_check_stops_at_docker_when_docker_is_down() -> None:
    def down(argv: tuple[str, ...], stdin_text: str | None = None) -> CommandResult:
        return CommandResult(
            exit_code=1, stdout="", stderr="Cannot connect to the Docker daemon"
        )

    checks = check_stack(down)
    assert [(check.name, check.passed) for check in checks] == [("docker", False)]
    assert "Cannot connect" in checks[0].detail


def test_up_and_down_drive_compose_with_the_generated_file() -> None:
    docker = ScriptedDocker(answers={})
    stream = io.StringIO()
    assert stack_up(docker, stream) == 0
    (up_call,) = docker.calls
    assert up_call[:6] == ("docker", "compose", "-p", COMPOSE_PROJECT, "-f", "-")
    assert up_call[6:] == ("up", "-d", "--wait")
    assert docker.stdin_texts == [compose_yaml()]

    leftovers = ScriptedDocker(
        answers={
            "docker ps": CommandResult(exit_code=0, stdout="c1\nc2\n", stderr=""),
            "network ls": CommandResult(exit_code=0, stdout="n1\n", stderr=""),
        }
    )
    stream = io.StringIO()
    assert stack_down(leftovers, stream, run_id="r-1", remove_volumes=False) == 0
    assert ("docker", "rm", "-f", "c1", "c2") in leftovers.calls
    assert ("docker", "network", "rm", "n1") in leftovers.calls
    assert ("docker", "ps", "-aq", "--filter", "label=vlah.run=r-1") in leftovers.calls
    assert not any(
        "compose" in call for call in leftovers.calls
    ), "one run: services stay up"
    assert "removed 2 container(s) and 1 network(s) of run r-1" in stream.getvalue()

    everything = ScriptedDocker(answers={})
    assert stack_down(everything, io.StringIO(), run_id=None, remove_volumes=True) == 0
    assert everything.calls[0][6:] == ("down", "--volumes")
    assert ("docker", "ps", "-aq", "--filter", "label=vlah.run") in everything.calls


def test_the_checked_in_compose_file_is_the_generated_one() -> None:
    """deploy/compose/compose.yaml is documentation of what vlah stack sends; this
    keeps the two from drifting apart."""
    checked_in = (REPO_ROOT / "deploy" / "compose" / "compose.yaml").read_text()
    assert checked_in == compose_yaml()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2g", 2 * GIBIBYTE),
        ("512m", 512 * 1024**2),
        ("1.5G", int(1.5 * GIBIBYTE)),
        ("4096", 4096),
    ],
)
def test_memory_sizes_read_as_docker_writes_them(text: str, expected: int) -> None:
    assert memory_bytes(text) == expected


def test_a_malformed_memory_size_is_refused() -> None:
    with pytest.raises(ValueError, match="2g or 512m"):
        memory_bytes("two gigs")


def test_kind_up_creates_installs_calico_waits_and_loads_the_image() -> None:
    docker = ScriptedDocker(
        answers={
            "get clusters": CommandResult(exit_code=0, stdout="other\n", stderr="")
        }
    )
    stream = io.StringIO()
    assert kind_up(docker, stream, "/Users/me", "vlah-worker:dev") == 0
    commands = [" ".join(call[:4]) for call in docker.calls]
    assert commands[0] == "kind get clusters"
    assert docker.calls[1][:5] == ("kind", "create", "cluster", "--config", "-")
    assert "disableDefaultCNI: true" in (docker.stdin_texts[1] or "")
    assert "hostPath: /Users/me" in (docker.stdin_texts[1] or "")
    assert any("calico" in " ".join(call) for call in docker.calls), "Calico is applied"
    assert docker.calls[-1] == (
        "kind",
        "load",
        "docker-image",
        "vlah-worker:dev",
        "--name",
        "vlah",
    )
    assert "ready with Calico" in stream.getvalue()

    existing = ScriptedDocker(
        answers={"get clusters": CommandResult(exit_code=0, stdout="vlah\n", stderr="")}
    )
    assert kind_up(existing, io.StringIO(), "/Users/me", "vlah-worker:dev") == 0
    assert not any(call[:3] == ("kind", "create", "cluster") for call in existing.calls)


def test_check_kind_names_the_missing_cluster_and_the_pod_probe() -> None:
    no_cluster = ScriptedDocker(
        answers={
            "cluster-info": CommandResult(
                exit_code=1, stdout="", stderr="connection refused"
            )
        }
    )
    checks = check_kind(no_cluster)
    assert [(check.name, check.passed) for check in checks] == [
        ("kind", True),
        ("kubectl", True),
        ("cluster", False),
    ]
    assert "vlah stack --kind up" in checks[-1].detail

    ready = ScriptedDocker(
        answers={
            "jsonpath={.items[*].status.phase}": CommandResult(
                exit_code=0, stdout="Running", stderr=""
            ),
            "crictl images": CommandResult(
                exit_code=0, stdout="sha256:abc\n", stderr=""
            ),
            "vlah-stack-probe": CommandResult(
                exit_code=0, stdout="resolved 192.168.65.254\nreached\n", stderr=""
            ),
        }
    )
    checks = check_kind(ready, gateway_url="http://host.docker.internal:4000")
    assert all(check.passed for check in checks), [check.detail for check in checks]
    assert [check.name for check in checks][-2:] == [
        "host from a pod",
        "model gateway from a pod",
    ]
