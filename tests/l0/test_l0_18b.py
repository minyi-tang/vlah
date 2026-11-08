"""L0-18b (v2.2 §14.1). Owner: W1-07 (#17); the Docker variant L-01 (#128).

The process is killed with kill -9 → the sidecar writes a stub claim on its behalf (D77).
"""

import os
import signal
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.fixtures.docker import image_missing_reason, running_docker_executor
from tests.fixtures.executor import (
    SLEEP_PROGRAM,
    START_PACK,
    TASK,
    WORKER,
    running_executor,
)
from vlah.adapters.executor.docker.client import DockerCli
from vlah.adapters.executor.docker.executor import DockerExecutor
from vlah.adapters.executor.local.traces import STUB_TAG
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import SystemClock

pytestmark = [pytest.mark.l0, pytest.mark.executor]

FINISH_TIMEOUT_SECONDS = 30.0


@pytest.mark.parametrize(
    "executor_kind",
    [
        "local",
        pytest.param("docker", marks=[pytest.mark.infra, pytest.mark.docker]),
    ],
)
def test_l0_18b_stub_claim_on_kill(tmp_path: Path, executor_kind: str) -> None:
    """The process is killed with kill -9 → the sidecar writes a stub claim on its behalf (D77)."""
    if executor_kind == "docker":
        reason = image_missing_reason()
        if reason is not None:
            pytest.skip(reason)
        else:
            pass
        context = running_docker_executor(
            tmp_path, SystemClock(), docker=DockerCli(), poll_seconds=0.25
        )
    else:
        context = running_executor(tmp_path, SystemClock(), poll_seconds=0.05)
    with context as harness:
        executor = harness.executor
        handle = executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        if isinstance(executor, DockerExecutor):
            # From outside the herd, as a docker kill by hand would be.
            subprocess.run(
                ["docker", "kill", executor.container_id(handle)],
                check=True,
                capture_output=True,
            )
        else:
            os.killpg(executor.process_id(handle), signal.SIGKILL)
        executor.session(handle).finished.wait(FINISH_TIMEOUT_SECONDS)
        exit_info = executor.wait(handle, 0.0)
        assert exit_info is not None and exit_info.was_killed

        claim_paths = harness.blackboard.list_paths(f"claims/{WORKER}/", None)
        (claim_path,) = claim_paths
        claim = parse_claim(harness.blackboard.read(claim_path, None).decode())
        assert (claim.type, claim.tags) == ("dead_end", (STUB_TAG,))
        pointer = claim.evidence[0].trace
        assert pointer is not None and pointer == f"traces/{WORKER}/{TASK.id}.ptr"
        record = yaml.safe_load(harness.blackboard.read(pointer, None))
        assert record["was_killed"] is True
        assert harness.objects.exists(record["objects"]["stdout"])
