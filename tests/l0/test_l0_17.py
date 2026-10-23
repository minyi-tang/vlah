"""L0-17 (v2.2 §14.1). Owner: W1-12 (#22).

A worker sandbox accesses a host outside the allowlist → denied (D75).
"""

import logging
from pathlib import Path

import pytest

from tests.fixtures.egress import (
    BLOCKED_HOST,
    PROBE_PROGRAM,
    PUBLIC_ADDRESS,
    make_sandbox,
    policy_for,
    serve_locally,
)
from vlah.adapters.executor.sandbox.sandbox import LAYER_NONE, detect_layer_two

pytestmark = [pytest.mark.l0, pytest.mark.egress]


def test_l0_17_egress_allowlist(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A worker sandbox accesses a host outside the allowlist → denied (D75)."""
    enforced = detect_layer_two() != LAYER_NONE
    tool_server = serve_locally()
    harness = make_sandbox(tmp_path, allow_unenforced=not enforced)
    environment = {
        "ALLOWED_URL": f"http://127.0.0.1:{tool_server.port}/hook/check",
        "BLOCKED_URL": f"http://{BLOCKED_HOST}/public-benchmark-answers",
        "OTHER_PORT": "9",
        "PUBLIC_ADDRESS": PUBLIC_ADDRESS,
        "PROTECTED_FILE": str(harness.protected / "HEAD"),
        "FREE_FILE": str(tmp_path / "free.txt"),
    }
    program = (
        "import os\n"
        + "\n".join(
            f"os.environ[{name!r}] = {value!r}" for name, value in environment.items()
        )
        + "\n"
        + PROBE_PROGRAM
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
