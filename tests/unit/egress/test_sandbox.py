"""Layer 2: a wrapped process cannot leave the allowlist even with raw sockets (D75)."""

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.egress import (
    BLOCKED_HOST,
    PROBE_PROGRAM,
    PUBLIC_ADDRESS,
    WORKER,
    make_sandbox,
    policy_for,
    serve_locally,
)
from vlah.adapters.executor.sandbox.sandbox import (
    LAYER_LINUX,
    LAYER_NONE,
    detect_layer_two,
)

pytestmark = pytest.mark.egress

ENFORCED_HERE = detect_layer_two() != LAYER_NONE


def test_wrap_refuses_without_layer_two_unless_overridden(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    strict = make_sandbox(tmp_path / "strict", layer_two=LAYER_NONE)
    with pytest.raises(RuntimeError, match="development only"):
        strict.sandbox.wrap(WORKER, ("true",), {}, policy_for(8100))
    lenient = make_sandbox(
        tmp_path / "lenient", allow_unenforced=True, layer_two=LAYER_NONE
    )
    with caplog.at_level(logging.WARNING):
        wrapped = lenient.sandbox.wrap(
            WORKER, ("true",), {"KEEP": "1"}, policy_for(8100)
        )
    assert wrapped.argv == ("true",), "no OS layer, only the proxy variables"
    assert wrapped.env["KEEP"] == "1"
    assert wrapped.env["HTTPS_PROXY"].startswith("http://127.0.0.1:")
    assert any("EGRESS NOT ENFORCED" in record.message for record in caplog.records)
    lenient.close()


@pytest.mark.skipif(not ENFORCED_HERE, reason="no OS-level enforcement on this host")
def test_a_wrapped_process_reaches_only_the_allowlist(tmp_path: Path) -> None:
    """The allowed local server works through the proxy; raw sockets and other
    names are blocked below it; the protected directory is read-only."""
    allowed = serve_locally()
    other = serve_locally()
    harness = make_sandbox(tmp_path)
    program = PROBE_PROGRAM
    environment = {
        "ALLOWED_URL": f"http://127.0.0.1:{allowed.port}/mcp",
        "BLOCKED_URL": f"http://{BLOCKED_HOST}/answers",
        "OTHER_PORT": str(other.port),
        "PUBLIC_ADDRESS": PUBLIC_ADDRESS,
        "PROTECTED_FILE": str(harness.protected / "blackboard.git" / "HEAD"),
        "FREE_FILE": str(tmp_path / "free.txt"),
    }
    (harness.protected / "blackboard.git").mkdir()
    try:
        wrapped_program = (
            "import os\n"
            + "\n".join(
                f"os.environ[{name!r}] = {value!r}"
                for name, value in environment.items()
            )
            + "\n"
            + program
        )
        report = harness.run_child(wrapped_program, policy_for(allowed.port))
    finally:
        harness.close()
        allowed.stop()
        other.stop()
    assert report["allowed_via_proxy"] == f"hello from {allowed.port}"
    assert str(report["blocked_name_via_proxy"]).startswith("blocked:")
    assert str(report["raw_other_local_port"]).startswith("blocked:"), "layer 2"
    assert str(report["raw_public_address"]).startswith("blocked:"), "layer 2"
    assert str(report["write_protected"]).startswith("blocked:")
    assert report["write_elsewhere"] == "written"
    assert allowed.paths == ["/mcp"] and other.paths == []
    assert [denied.host for denied in harness.sandbox.denied()] == [BLOCKED_HOST]


TRAPS_SIGTERM = """
import signal, sys, time
def on_term(signum, frame):
    print("got term", flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
print("ready", flush=True)
time.sleep(60)
"""


@pytest.mark.skipif(detect_layer_two() != LAYER_LINUX, reason="the Linux launcher")
def test_the_linux_launcher_lets_the_agent_use_its_grace_period(tmp_path: Path) -> None:
    """The executor signals the process group; the launcher must outlive its child
    and return the child's own exit code."""
    harness = make_sandbox(tmp_path)
    try:
        wrapped = harness.sandbox.wrap(
            WORKER, (sys.executable, "-c", TRAPS_SIGTERM), {}, policy_for(8100)
        )
        process = subprocess.Popen(
            list(wrapped.argv),
            env={"PATH": "/usr/bin:/bin", **wrapped.env},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)
    finally:
        harness.close()
    assert process.returncode == 0, stderr
    assert "got term" in stdout
