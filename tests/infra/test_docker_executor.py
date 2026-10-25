"""The Docker executor against the daemon: the port's check, the worker boundary,
limits read back from docker inspect, and nothing left behind (#128)."""

import json
import subprocess
from pathlib import Path

import pytest

from tests.fixtures.docker import (
    QUICK_PROGRAM,
    SLEEP_PROGRAM,
    ContainerFacingServer,
    running_docker_executor,
)
from tests.fixtures.executor import START_PACK, TASK, WORKER, RealTimer
from vlah.adapters.executor.docker.client import DockerCli
from vlah.adapters.executor.docker.egress import (
    AGENT_LABEL,
    ATTEMPT_LABEL,
    IMAGE_PYTHON,
    RUN_LABEL,
    TASK_LABEL,
    RunEgress,
    run_label,
)
from vlah.adapters.executor.docker.executor import ContainerResources
from vlah.core.types.clock import SystemClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy, HostRule, Params
from vlah.testing.boundary import (
    BLOCKED_HOST,
    BoundaryExpectations,
    check_worker_boundary,
)
from vlah.testing.conformance import ExecutorCheckInputs, check_executor

pytestmark = [
    pytest.mark.infra,
    pytest.mark.executor,
    pytest.mark.docker,
    pytest.mark.timeout(600),
]

CONTAINER_HOST = "host.docker.internal"
HEARTBEAT_SECONDS = 1.0
POLL_SECONDS = 0.25
MEBIBYTE = 1024 * 1024


def docker_output(*arguments: str) -> str:
    return subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, check=True
    ).stdout


def leftovers(run_id: str) -> tuple[list[str], list[str]]:
    """Containers and networks that still carry the run's label."""
    label = run_label(run_id)
    containers = docker_output("ps", "--all", "--quiet", "--filter", f"label={label}")
    networks = docker_output("network", "ls", "--quiet", "--filter", f"label={label}")
    return containers.split(), networks.split()


def tool_policy(port: int) -> EgressPolicy:
    return EgressPolicy(
        allow=(HostRule(host=CONTAINER_HOST, ports=(port,), purpose="tools"),)
    )


def test_check_executor_against_real_containers(
    tmp_path: Path, worker_image: str
) -> None:
    """Heartbeats while the agent only sleeps, kill, a transcript per session, one
    sandbox per task: the port's check, with real containers and real time."""
    with running_docker_executor(
        tmp_path,
        SystemClock(),
        docker=DockerCli(),
        image=worker_image,
        params=Params(heartbeat_interval_seconds=HEARTBEAT_SECONDS),
        poll_seconds=POLL_SECONDS,
    ) as harness:
        check_executor(
            harness.executor,
            ExecutorCheckInputs(
                long_running_spec=harness.spec(SLEEP_PROGRAM),
                quick_spec=harness.spec(QUICK_PROGRAM, agent=ActorId("worker-02")),
                task=TASK,
                start_pack=START_PACK,
                heartbeat_interval_seconds=HEARTBEAT_SECONDS,
            ),
            RealTimer(),
        )


def test_the_boundary_holds_from_inside_the_container(
    tmp_path: Path, worker_image: str
) -> None:
    """The probe runs through docker exec in a worker's own container: the herd's
    files are nowhere on its filesystem, the tool server answers only through the
    proxy, raw sockets go nowhere, and the clones name no repository (D63, D67, D68, D75).
    """
    tool_server = ContainerFacingServer()
    cli = DockerCli()
    try:
        with running_docker_executor(
            tmp_path,
            SystemClock(),
            docker=cli,
            image=worker_image,
            poll_seconds=POLL_SECONDS,
        ) as harness:
            handle = harness.executor.start(
                harness.spec(SLEEP_PROGRAM, egress=tool_policy(tool_server.port)),
                TASK,
                START_PACK,
                harness.sink,
            )
            container = harness.executor.container_id(handle)

            def run_in_sandbox(argv: tuple[str, ...]) -> tuple[int, str]:
                return cli.execute(container, argv)

            report = check_worker_boundary(
                run_in_sandbox,
                BoundaryExpectations(
                    python_executable=IMAGE_PYTHON,
                    tool_url=f"http://{CONTAINER_HOST}:{tool_server.port}/hook/check",
                    hidden_paths=(
                        str(tmp_path / "runs" / "run-1" / "blackboard.git" / "HEAD"),
                        str(harness.target / ".git" / "HEAD"),
                    ),
                    search_roots=("/",),
                    push_url=harness.clone_url,
                    allowed_variables=(),
                    is_enforced_below_proxy=True,
                ),
            )
            assert report["git_remotes"] == {"target": "", "blackboard": ""}
            assert tool_server.paths == ["/hook/check"], "reached through the proxy"
            denied = [
                (item.agent, item.host, item.port) for item in harness.executor.denied()
            ]
            assert denied == [(WORKER, BLOCKED_HOST, 80)]
            assert (harness.log_root / f"{WORKER}-denied.tsv").is_file()
    finally:
        tool_server.stop()


def test_limits_and_labels_apply_and_close_leaves_nothing(
    tmp_path: Path, worker_image: str
) -> None:
    run_id = "run-1"
    with running_docker_executor(
        tmp_path,
        SystemClock(),
        docker=DockerCli(),
        image=worker_image,
        poll_seconds=POLL_SECONDS,
        resources=ContainerResources(cpus=1.0, memory="256m"),
    ) as harness:
        handle = harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        inspected = json.loads(
            docker_output(
                "inspect",
                "--format",
                '{"memory": {{.HostConfig.Memory}}, "nano_cpus": {{.HostConfig.NanoCpus}}, '
                '"labels": {{json .Config.Labels}}, "mounts": {{json .Mounts}}}',
                harness.executor.container_id(handle),
            )
        )
        assert inspected["memory"] == 256 * MEBIBYTE
        assert inspected["nano_cpus"] == 1_000_000_000
        assert inspected["labels"] == {
            RUN_LABEL: run_id,
            AGENT_LABEL: WORKER,
            TASK_LABEL: TASK.id,
            ATTEMPT_LABEL: "1",
        }
        assert [mount["Destination"] for mount in inspected["mounts"]] == ["/work"]
        containers, networks = leftovers(run_id)
        assert len(containers) == 2 and len(networks) == 1, "the worker and the proxy"
    assert leftovers(run_id) == ([], [])


def test_a_crashed_control_plane_leaves_containers_their_label_finds(
    tmp_path: Path, worker_image: str
) -> None:
    """Without close(), the containers stay; vlah stack down (#129) removes them by
    the run's label, which RunEgress.close does here."""
    run_id = "run-crash"
    cli = DockerCli()
    with running_docker_executor(
        tmp_path,
        SystemClock(),
        docker=cli,
        image=worker_image,
        poll_seconds=POLL_SECONDS,
        run_id=run_id,
        close_on_exit=False,
    ) as harness:
        harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        harness.executor.stopping.set()
    try:
        containers, networks = leftovers(run_id)
        assert len(containers) == 2 and len(networks) == 1
    finally:
        RunEgress(
            run_id=run_id, image=worker_image, log_root=harness.log_root, docker=cli
        ).close()
    assert leftovers(run_id) == ([], [])
