"""check_worker_boundary: it passes a clean sandbox and names what leaks in a dirty one (#127)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.egress import serve_locally
from vlah.testing.boundary import (
    BoundaryExpectations,
    RunInSandbox,
    check_worker_boundary,
)

pytestmark = pytest.mark.harness

PROBE_TIMEOUT_SECONDS = 60.0


def subprocess_sandbox(environment: dict[str, str], cwd: Path) -> RunInSandbox:
    """The local case: the probe runs as a subprocess with exactly this environment."""

    def run(argv: tuple[str, ...]) -> tuple[int, str]:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        return completed.returncode, completed.stdout + completed.stderr

    return run


def test_a_clean_sandbox_passes_and_a_leaky_one_is_named(tmp_path: Path) -> None:
    tool_server = serve_locally()
    clean_environment = {
        "PATH": os.environ.get("PATH", ""),
        "VLAH_TOOL_TOKEN": "per-agent-token",
    }
    hidden = tmp_path / "problem" / "herd.yaml"
    hidden.parent.mkdir()
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    expectations = BoundaryExpectations(
        python_executable=sys.executable,
        tool_url=f"http://127.0.0.1:{tool_server.port}/hook/check",
        hidden_paths=(str(hidden),),
        search_roots=(str(sandbox),),
        # Only the environment is controlled here: no proxy, no OS layer.
        is_enforced_below_proxy=False,
    )
    try:
        report = check_worker_boundary(
            subprocess_sandbox(clean_environment, sandbox), expectations
        )
        assert report["git_remotes"] == {"target": "absent", "blackboard": "absent"}

        hidden.write_text("judges: {}\n")
        leaky_environment = {
            **clean_environment,
            "VLAH_PUSH_TOKEN": "the push credential",
            "GATEWAY_API_KEY": "secret",
        }
        with pytest.raises(AssertionError, match="hidden files readable"):
            check_worker_boundary(
                subprocess_sandbox(leaky_environment, sandbox), expectations
            )
        hidden.unlink()
        with pytest.raises(AssertionError, match="GATEWAY_API_KEY"):
            check_worker_boundary(
                subprocess_sandbox(leaky_environment, sandbox), expectations
            )
    finally:
        tool_server.stop()


def test_a_herd_yaml_inside_the_search_roots_is_a_breach(tmp_path: Path) -> None:
    tool_server = serve_locally()
    sandbox = tmp_path / "sandbox"
    (sandbox / "nested" / "private").mkdir(parents=True)
    (sandbox / "nested" / "herd.yaml").write_text("judges: {}\n")
    expectations = BoundaryExpectations(
        python_executable=sys.executable,
        tool_url=f"http://127.0.0.1:{tool_server.port}/",
        search_roots=(str(sandbox),),
        is_enforced_below_proxy=False,
    )
    try:
        with pytest.raises(AssertionError, match="herd.yaml in the sandbox"):
            check_worker_boundary(
                subprocess_sandbox({"PATH": os.environ.get("PATH", "")}, sandbox),
                expectations,
            )
    finally:
        tool_server.stop()
