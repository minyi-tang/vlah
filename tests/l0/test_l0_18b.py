"""L0-18b (v2.2 §14.1). Owner: W1-07 (#17).

The process is killed with kill -9 → the sidecar writes a stub claim on its behalf (D77).
"""

import os
import signal
from pathlib import Path

import pytest
import yaml

from tests.fixtures.executor import (
    SLEEP_PROGRAM,
    START_PACK,
    TASK,
    WORKER,
    running_executor,
)
from vlah.adapters.executor.local.traces import STUB_TAG
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import SystemClock

pytestmark = [pytest.mark.l0, pytest.mark.executor]


def test_l0_18b_stub_claim_on_kill(tmp_path: Path) -> None:
    """The process is killed with kill -9 → the sidecar writes a stub claim on its behalf (D77)."""
    with running_executor(tmp_path, SystemClock(), poll_seconds=0.05) as harness:
        handle = harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        os.killpg(harness.executor.process_id(handle), signal.SIGKILL)
        harness.executor.session(handle).finished.wait(10.0)
        exit_info = harness.executor.wait(handle, 0.0)
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
