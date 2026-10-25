"""The worker boundary, checked from inside the sandbox (D63, D67, D68, D75).

Every executor runs the scripted worker's probe where its agents run and hands the
report to check_worker_boundary: no hidden file is readable by any path, no push
credential or other secret is in the environment, the only way out is the
allowlist proxy, the tool server answers, a push is refused, and the clones name no
repository of the herd. The caller says how to run a command in the sandbox: a
subprocess for the local executor, an exec into the container or pod for the others.
"""

import json
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

from vlah.testing.scripted_worker import MODULE, TOOL_TOKEN_VARIABLE

BLOCKED_HOST = "blocked.example.invalid"
PUBLIC_ADDRESS = "1.1.1.1"
NO_REMOTES = ("", "absent")


class RunInSandbox(Protocol):
    def __call__(self, argv: tuple[str, ...]) -> tuple[int, str]:
        """Run a command where the worker runs; return its exit code and stdout."""
        ...


@dataclass(frozen=True, kw_only=True)
class BoundaryExpectations:
    """What the probe is told to try, and which findings count as a breach.

    is_enforced_below_proxy is False where only the proxy variables limit egress
    (development, or an executor without an OS layer): the raw-socket probe is then
    reported but not asserted.
    """

    python_executable: str
    tool_url: str
    blocked_host: str = BLOCKED_HOST
    public_address: str = PUBLIC_ADDRESS
    hidden_paths: tuple[str, ...] = ()
    search_roots: tuple[str, ...] = ()
    push_url: str | None = None
    allowed_variables: tuple[str, ...] = (TOOL_TOKEN_VARIABLE,)
    is_enforced_below_proxy: bool = True
    output_path: str | None = None


def probe_argv(expectations: BoundaryExpectations) -> tuple[str, ...]:
    argv = [
        expectations.python_executable,
        "-m",
        MODULE,
        "--script",
        "probe",
        "--tool-url",
        expectations.tool_url,
        "--blocked-host",
        expectations.blocked_host,
        "--public-address",
        expectations.public_address,
    ]
    for path in expectations.hidden_paths:
        argv.extend(("--hidden-path", path))
    for root in expectations.search_roots:
        argv.extend(("--search-root", root))
    for name in expectations.allowed_variables:
        argv.extend(("--allowed-variable", name))
    if expectations.push_url is not None:
        argv.extend(("--push-url", expectations.push_url))
    else:
        pass
    if expectations.output_path is not None:
        argv.extend(("--output", expectations.output_path))
    else:
        pass
    return tuple(argv)


def parse_report(stdout: str) -> dict[str, JsonValue]:
    """The probe's report is the last non-empty line of its output."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("the probe printed nothing")
    else:
        report: JsonValue = json.loads(lines[-1])
        if isinstance(report, dict):
            return {str(key): value for key, value in report.items()}
        else:
            raise AssertionError(f"the probe printed no JSON object: {lines[-1]!r}")


def check_worker_boundary(
    run_in_sandbox: RunInSandbox, expectations: BoundaryExpectations
) -> dict[str, JsonValue]:
    """Assert the boundary holds; return the report for any further assertions."""
    exit_code, stdout = run_in_sandbox(probe_argv(expectations))
    assert exit_code == 0, f"the probe exited {exit_code}: {stdout[-2000:]}"
    report = parse_report(stdout)
    assert (
        report["hidden_readable"] == []
    ), f"hidden files readable: {report['hidden_readable']}"
    assert (
        report["herd_yaml_found"] == []
    ), f"herd.yaml in the sandbox: {report['herd_yaml_found']}"
    assert (
        report["private_found"] == []
    ), f"private/ in the sandbox: {report['private_found']}"
    assert (
        report["credential_variables"] == []
    ), f"credentials in the environment: {report['credential_variables']} (D68)"
    assert (
        report["push_token_present"] is False
    ), "VLAH_PUSH_TOKEN reached the worker (D68)"
    assert str(report["blocked_via_proxy"]).startswith(
        "blocked:"
    ), f"a host outside the allowlist was reached: {report['blocked_via_proxy']} (D75)"
    if expectations.is_enforced_below_proxy:
        assert str(report["raw_public_address"]).startswith(
            "blocked:"
        ), f"a raw socket left the sandbox: {report['raw_public_address']} (D75)"
    else:
        pass
    assert (
        report["tool_url"] == "reached"
    ), f"the tool server did not answer: {report['tool_url']}"
    if expectations.push_url is not None:
        assert str(report["git_push"]).startswith(
            "rejected:"
        ), f"the worker could push: {report['git_push']} (D68)"
    else:
        pass
    remotes = report["git_remotes"]
    assert isinstance(remotes, dict)
    for clone, listing in remotes.items():
        assert listing in NO_REMOTES, f"{clone} names a remote: {listing!r} (#45)"
    return report
