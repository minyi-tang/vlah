"""L0-17 (v2.2 §14.1). Owner: W1-12 (#22); the Docker variant L-01 (#128).

A worker sandbox accesses a host outside the allowlist → denied (D75).
"""

import json
import logging
from pathlib import Path

import pytest

from tests.fixtures.docker import (
    ContainerFacingServer,
    image_missing_reason,
    running_docker_executor,
)
from tests.fixtures.egress import (
    BLOCKED_HOST,
    PROBE_PROGRAM,
    PUBLIC_ADDRESS,
    make_sandbox,
    policy_for,
    serve_locally,
)
from tests.fixtures.executor import START_PACK, TASK, WORKER
from vlah.adapters.executor.docker.client import DockerCli
from vlah.adapters.executor.sandbox.sandbox import LAYER_NONE, detect_layer_two
from vlah.core.types.clock import SystemClock
from vlah.core.types.params import EgressPolicy, HostRule

pytestmark = [pytest.mark.l0, pytest.mark.egress]

CONTAINER_HOST = "host.docker.internal"
CONTAINER_EXIT_TIMEOUT_SECONDS = 60.0


def probe_program(environment: dict[str, str]) -> str:
    return (
        "import os\n"
        + "\n".join(
            f"os.environ[{name!r}] = {value!r}" for name, value in environment.items()
        )
        + "\n"
        + PROBE_PROGRAM
    )


def test_l0_17_egress_allowlist_in_a_process_sandbox(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A worker sandbox accesses a host outside the allowlist → denied (D75)."""
    enforced = detect_layer_two() != LAYER_NONE
    tool_server = serve_locally()
    harness = make_sandbox(tmp_path, allow_unenforced=not enforced)
    program = probe_program(
        {
            "ALLOWED_URL": f"http://127.0.0.1:{tool_server.port}/hook/check",
            "BLOCKED_URL": f"http://{BLOCKED_HOST}/public-benchmark-answers",
            "OTHER_PORT": "9",
            "PUBLIC_ADDRESS": PUBLIC_ADDRESS,
            "PROTECTED_FILE": str(harness.protected / "HEAD"),
            "FREE_FILE": str(tmp_path / "free.txt"),
        }
    )
    try:
        with caplog.at_level(logging.WARNING):
            report = harness.run_child(program, policy_for(tool_server.port))
    finally:
        harness.close()
        tool_server.stop()
    assert str(report["allowed_via_proxy"]).startswith("hello"), "the herd's own host"
    assert str(report["blocked_name_via_proxy"]).startswith("blocked:"), "denied"
    (denied,) = harness.sandbox.denied()
    assert (denied.host, denied.port) == (BLOCKED_HOST, 80)
    if enforced:
        assert str(report["raw_public_address"]).startswith(
            "blocked:"
        ), "below the proxy"
    else:
        assert any("EGRESS NOT ENFORCED" in record.message for record in caplog.records)


@pytest.mark.infra
@pytest.mark.docker
def test_l0_17_egress_allowlist_in_a_container(tmp_path: Path) -> None:
    """A worker sandbox accesses a host outside the allowlist → denied (D75): the
    container's network has no route out, and the proxy container refuses the name."""
    reason = image_missing_reason()
    if reason is not None:
        pytest.skip(reason)
    else:
        pass
    tool_server = ContainerFacingServer()
    policy = EgressPolicy(
        allow=(
            HostRule(host=CONTAINER_HOST, ports=(tool_server.port,), purpose="tools"),
        )
    )
    program = probe_program(
        {
            "ALLOWED_URL": f"http://{CONTAINER_HOST}:{tool_server.port}/hook/check",
            "BLOCKED_URL": f"http://{BLOCKED_HOST}/public-benchmark-answers",
            "OTHER_PORT": "9",
            "PUBLIC_ADDRESS": PUBLIC_ADDRESS,
            # The run directory is not in the container at all.
            "PROTECTED_FILE": str(
                tmp_path / "runs" / "run-1" / "blackboard.git" / "HEAD"
            ),
            "FREE_FILE": "/work/free.txt",
        }
    )
    try:
        with running_docker_executor(
            tmp_path, SystemClock(), docker=DockerCli(), poll_seconds=0.25
        ) as harness:
            harness.claims.written[(WORKER, TASK.id)] = ("c-worker-01-00001",)
            handle = harness.executor.start(
                harness.spec(program, egress=policy), TASK, START_PACK, harness.sink
            )
            exit_info = harness.executor.wait(handle, CONTAINER_EXIT_TIMEOUT_SECONDS)
            assert exit_info is not None and exit_info.return_code == 0
            lines = harness.executor.session(handle).container.logs.stdout.read_text()
            report = json.loads(lines.strip().splitlines()[-1])
            (denied,) = harness.executor.denied()
    finally:
        tool_server.stop()
    assert str(report["allowed_via_proxy"]).startswith("hello"), "the herd's own host"
    assert str(report["blocked_name_via_proxy"]).startswith("blocked:"), "denied"
    assert str(report["raw_public_address"]).startswith("blocked:"), "no route out"
    assert str(report["raw_other_local_port"]).startswith("blocked:")
    assert str(report["write_protected"]).startswith("blocked:"), "not mounted"
    assert report["write_elsewhere"] == "written"
    assert (denied.agent, denied.host, denied.port) == (WORKER, BLOCKED_HOST, 80)
