"""The Docker executor over a fake Docker: what it asks for, and how sessions end (D32, D67, D68, D75, D77)."""

import logging
from pathlib import Path

import pytest
import yaml

from tests.fixtures.docker import (
    QUICK_PROGRAM,
    RUN_ID,
    SIGKILL_EXIT_CODE,
    SLEEP_PROGRAM,
    WORKER_IMAGE,
    FakeDockerClient,
    running_docker_executor,
)
from tests.fixtures.executor import START_PACK, TASK, WORKER
from vlah.adapters.executor.docker.client import BindMount
from vlah.adapters.executor.docker.egress import (
    AGENT_LABEL,
    ATTEMPT_LABEL,
    BRIDGE_NETWORK,
    FIRST_PROXY_PORT,
    HOST_GATEWAY_ALIAS,
    LOGS_MOUNT,
    RUN_LABEL,
    TASK_LABEL,
)
from vlah.adapters.executor.docker.executor import (
    HOME_DIRECTORY,
    WORK_DIRECTORY,
    ContainerResources,
)
from vlah.adapters.executor.local.traces import RESTART_HINT, STUB_TAG
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import EgressPolicy, HostRule
from vlah.ports.executor import ExitInfo

pytestmark = [pytest.mark.executor, pytest.mark.docker]

POLICY = EgressPolicy(
    allow=(HostRule(host="host.docker.internal", ports=(8200,), purpose="tools"),)
)
INTERRUPT = Interrupt(
    id="i-1",
    agent=WORKER,
    kind="hard",
    rule=2,
    reason="your target claim was refuted",
    written_ts=0.0,
)


def claims_on_blackboard(paths: tuple[str, ...]) -> list[str]:
    return [path for path in paths if path.startswith("claims/")]


def test_a_session_is_a_container_with_only_its_sandbox_mounted(tmp_path: Path) -> None:
    """Requirements 2 to 7 of #128, read back from what docker run was asked for."""
    docker = FakeDockerClient()
    with running_docker_executor(tmp_path, FakeClock(), docker=docker) as harness:
        handle = harness.executor.start(
            harness.spec(SLEEP_PROGRAM, env={"KEEP": "1"}, egress=POLICY),
            TASK,
            START_PACK,
            harness.sink,
        )
        sandbox = harness.sandbox()
        (worker,) = docker.workers()
        spec = worker.spec
        assert spec.image == WORKER_IMAGE
        assert spec.argv == ("python", "-c", SLEEP_PROGRAM)
        assert spec.mounts == (
            BindMount(source=sandbox, destination=WORK_DIRECTORY, read_only=False),
        ), "nothing but the sandbox"
        assert spec.working_directory == WORK_DIRECTORY
        assert (spec.cpus, spec.memory) == (2.0, "2g")
        assert spec.labels == {
            RUN_LABEL: RUN_ID,
            AGENT_LABEL: WORKER,
            TASK_LABEL: TASK.id,
            ATTEMPT_LABEL: "1",
        }
        environment = spec.environment
        assert environment["KEEP"] == "1"
        assert environment["HOME"] == HOME_DIRECTORY
        assert environment["VLAH_SANDBOX"] == WORK_DIRECTORY
        assert environment["VLAH_TARGET"] == f"{WORK_DIRECTORY}/target"
        assert environment["VLAH_TRANSCRIPT"].startswith(f"{WORK_DIRECTORY}/.vlah/")
        assert "PATH" not in environment, "nothing of the host is inherited"
        proxy_url = f"http://vlah-{RUN_ID}-proxy:{FIRST_PROXY_PORT}"
        assert environment["HTTP_PROXY"] == environment["HTTPS_PROXY"] == proxy_url
        assert environment["NO_PROXY"] == "", "the tool server goes through the proxy"
        assert (sandbox / ".home").is_dir()
        assert harness.executor.transcript_path(handle) == sandbox / ".vlah" / (
            f"{handle.handle_id}-1.transcript.jsonl"
        )

        (network,) = docker.networks.values()
        assert network.name == spec.network == f"vlah-{RUN_ID}"
        assert network.internal and network.labels == {RUN_LABEL: RUN_ID}
        proxy = docker.proxy()
        assert proxy.networks == [BRIDGE_NETWORK, network.name]
        assert proxy.spec.extra_hosts == (HOST_GATEWAY_ALIAS,)
        assert proxy.spec.mounts == (
            BindMount(source=harness.log_root, destination=LOGS_MOUNT, read_only=False),
        )
        assert proxy.spec.labels[RUN_LABEL] == RUN_ID
        request = harness.listener_request()
        assert (request.port, request.generation, request.policy) == (
            FIRST_PROXY_PORT,
            1,
            POLICY,
        )
        assert harness.sink.beats == [(WORKER, (), 0.0)], "the first beat at start"


def test_each_agent_keeps_its_own_proxy_port_across_restarts(tmp_path: Path) -> None:
    docker = FakeDockerClient()
    with running_docker_executor(tmp_path, FakeClock(), docker=docker) as harness:
        first = harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        harness.executor.start(
            harness.spec(SLEEP_PROGRAM, agent=ActorId("worker-02")),
            TASK,
            START_PACK,
            harness.sink,
        )
        harness.executor.interrupt(first, INTERRUPT)
        ports = [
            worker.spec.environment["HTTP_PROXY"].rpartition(":")[2]
            for worker in docker.workers()
            if worker.is_running
        ]
        assert sorted(ports) == [str(FIRST_PROXY_PORT), str(FIRST_PROXY_PORT + 1)]
        assert harness.listener_request().generation == 2, "a new listener, same port"
        assert harness.listener_request(ActorId("worker-02")).generation == 1


@pytest.mark.parametrize(
    ("exit_code", "oom_killed", "expected"),
    [
        (0, False, ExitInfo(return_code=0, was_killed=False)),
        (3, False, ExitInfo(return_code=3, was_killed=False)),
        (SIGKILL_EXIT_CODE, False, ExitInfo(return_code=None, was_killed=True)),
        (0, True, ExitInfo(return_code=None, was_killed=True)),
    ],
    ids=["clean", "failed", "signal", "oom"],
)
def test_exit_information_comes_from_the_containers_state(
    tmp_path: Path, exit_code: int, oom_killed: bool, expected: ExitInfo
) -> None:
    docker = FakeDockerClient()
    with running_docker_executor(tmp_path, FakeClock(), docker=docker) as harness:
        harness.claims.written[(WORKER, TASK.id)] = ("c-worker-01-00001",)
        handle = harness.executor.start(
            harness.spec(QUICK_PROGRAM), TASK, START_PACK, harness.sink
        )
        (worker,) = docker.workers()
        worker.stdout, worker.stderr = b"out\n", b"err\n"
        docker.exit(worker.container_id, exit_code, oom_killed)
        assert harness.executor.wait(handle, 1.0) == expected
        assert worker.container_id in docker.removed, "an ended container is removed"
        logs = harness.executor.session(handle).container.logs
        assert (logs.stdout.read_bytes(), logs.stderr.read_bytes()) == (
            b"out\n",
            b"err\n",
        )
        pointer = f"traces/{WORKER}/{TASK.id}.ptr"
        record = yaml.safe_load(harness.blackboard.read(pointer, None))
        assert record["was_killed"] is expected.was_killed
        assert harness.objects.exists(record["objects"]["stdout"])


def test_an_exit_without_a_claim_restarts_once_then_leaves_a_stub(
    tmp_path: Path,
) -> None:
    docker = FakeDockerClient()
    with running_docker_executor(tmp_path, FakeClock(), docker=docker) as harness:
        handle = harness.executor.start(
            harness.spec(QUICK_PROGRAM), TASK, START_PACK, harness.sink
        )
        (first,) = docker.workers()
        docker.exit(first.container_id, 0)
        assert harness.executor.is_alive(handle), "restarted once"
        (second,) = docker.workers()
        assert second.spec.labels[ATTEMPT_LABEL] == "2"
        assert second.spec.mounts == first.spec.mounts, "the same sandbox"
        assert RESTART_HINT in (harness.sandbox() / "CONTEXT.md").read_text()
        docker.exit(second.container_id, 0)
        exit_info = harness.executor.wait(handle, 1.0)
        assert exit_info is not None and exit_info.return_code == 0
        paths = harness.blackboard.list_paths("", None)
        assert {
            f"traces/{WORKER}/{TASK.id}.ptr",
            f"traces/{WORKER}/{TASK.id}.2.ptr",
        } <= set(paths)
        (claim_path,) = claims_on_blackboard(paths)
        claim = parse_claim(harness.blackboard.read(claim_path, None).decode())
        assert (claim.type, claim.tags) == ("dead_end", (STUB_TAG,))
        assert docker.workers() == [], "no container outlives its session"


def test_an_interrupt_stops_the_container_and_starts_another_on_the_sandbox(
    tmp_path: Path,
) -> None:
    docker = FakeDockerClient()
    with running_docker_executor(
        tmp_path, FakeClock(), docker=docker, grace_seconds=7.0
    ) as harness:
        handle = harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        (first,) = docker.workers()
        restarted = harness.executor.interrupt(handle, INTERRUPT)
        assert docker.stops == [(first.container_id, 7.0)], "docker stop -t <grace>"
        old_exit = harness.executor.wait(handle, 0.0)
        assert old_exit is not None and old_exit.was_killed
        assert restarted.handle_id != handle.handle_id
        assert harness.executor.is_alive(restarted)
        (current,) = docker.workers()
        assert current.spec.mounts == first.spec.mounts
        assert harness.executor.session(restarted).start_pack.interrupt == INTERRUPT
        context = (harness.sandbox() / "CONTEXT.md").read_text()
        assert context.startswith("# Task") and INTERRUPT.reason in context
        assert len(claims_on_blackboard(harness.blackboard.list_paths("", None))) == 1


def test_close_removes_everything_with_the_runs_label(tmp_path: Path) -> None:
    """A crashed control plane leaves containers only their label finds; close()
    sweeps those too, and never another run's."""
    docker = FakeDockerClient()
    other_run = docker.add_orphan("vlah-run-2-orphan", "run-2")
    with running_docker_executor(tmp_path, FakeClock(), docker=docker) as harness:
        harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        orphan = docker.add_orphan("vlah-run-1-left-behind", RUN_ID)
        assert len(docker.containers_with_label(f"{RUN_LABEL}={RUN_ID}")) == 3
    assert docker.containers_with_label(f"{RUN_LABEL}={RUN_ID}") == ()
    assert docker.networks_with_label(f"{RUN_LABEL}={RUN_ID}") == ()
    assert orphan in docker.removed
    assert other_run in docker.containers


def test_read_only_mounts_are_refused_under_the_problem_or_the_run(
    tmp_path: Path,
) -> None:
    problem = tmp_path / "problem"
    (problem / "private").mkdir(parents=True)
    wrapper = tmp_path / "gateway" / "claude-ghcp"
    wrapper.parent.mkdir()
    wrapper.write_text("#!/bin/sh\n")
    refused = (problem, tmp_path / "runs" / RUN_ID)
    for host_path in (
        problem / "private",
        tmp_path / "runs" / RUN_ID / "blackboard.git",
    ):
        with pytest.raises(ValueError, match="worker must not see"):
            with running_docker_executor(
                tmp_path,
                FakeClock(),
                mounts_read_only={host_path: "/opt/x"},
                refused_mount_roots=refused,
            ):
                pass
    with pytest.raises(ValueError, match="does not exist"):
        with running_docker_executor(
            tmp_path, FakeClock(), mounts_read_only={tmp_path / "missing": "/opt/x"}
        ):
            pass
    docker = FakeDockerClient()
    with running_docker_executor(
        tmp_path,
        FakeClock(),
        docker=docker,
        mounts_read_only={wrapper: "/opt/gateway/claude-ghcp"},
        refused_mount_roots=refused,
        resources=ContainerResources(cpus=1.5, memory="512m"),
    ) as harness:
        harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        (worker,) = docker.workers()
        assert worker.spec.mounts[1:] == (
            BindMount(
                source=wrapper.resolve(),
                destination="/opt/gateway/claude-ghcp",
                read_only=True,
            ),
        )
        assert (worker.spec.cpus, worker.spec.memory) == (1.5, "512m")


def test_unenforced_egress_gives_the_network_a_way_out_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    docker = FakeDockerClient()
    with running_docker_executor(
        tmp_path, FakeClock(), docker=docker, allow_unenforced=True
    ) as harness:
        with caplog.at_level(logging.WARNING):
            harness.executor.start(
                harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
            )
        (network,) = docker.networks.values()
        assert not network.internal
        (worker,) = docker.workers()
        assert worker.spec.environment["HTTP_PROXY"].startswith(
            "http://"
        ), "layer 1 stays"
        assert any("EGRESS NOT ENFORCED" in record.message for record in caplog.records)


def test_the_worker_environment_holds_no_push_token(tmp_path: Path) -> None:
    with running_docker_executor(tmp_path, FakeClock()) as harness:
        with pytest.raises(ValueError, match="VLAH_PUSH_TOKEN"):
            harness.executor.start(
                harness.spec(QUICK_PROGRAM, env={"VLAH_PUSH_TOKEN": "s3cret"}),
                TASK,
                START_PACK,
                harness.sink,
            )


def test_denied_connections_are_read_from_the_proxys_logs(tmp_path: Path) -> None:
    with running_docker_executor(tmp_path, FakeClock()) as harness:
        harness.log_root.mkdir(parents=True, exist_ok=True)
        (harness.log_root / "worker-02-denied.tsv").write_text(
            "12.5\tblocked.example.invalid\t80\n"
        )
        (harness.log_root / "worker-01-denied.tsv").write_text("3.0\t1.1.1.1\t443\n")
        denied = harness.executor.denied()
        assert [
            (item.timestamp_seconds, item.agent, item.host, item.port)
            for item in denied
        ] == [
            (3.0, "worker-01", "1.1.1.1", 443),
            (12.5, "worker-02", "blocked.example.invalid", 80),
        ]


def test_a_dead_proxy_container_fails_the_start_instead_of_waiting(
    tmp_path: Path,
) -> None:
    docker = FakeDockerClient()
    with running_docker_executor(tmp_path, FakeClock(), docker=docker) as harness:
        harness.executor.egress.ready_timeout_seconds = 2.0
        harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        proxy = docker.proxy()
        proxy.stderr = b"Traceback: bind failed\n"
        docker.exit(proxy.container_id, 1)
        with pytest.raises(RuntimeError, match="bind failed"):
            harness.executor.start(
                harness.spec(SLEEP_PROGRAM, agent=ActorId("worker-02")),
                TASK,
                START_PACK,
                harness.sink,
            )
