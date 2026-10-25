"""The worker image: no credential, no hidden material, the tools run as a non-root user (#127)."""

import json
import re
import subprocess

import pytest

from vlah.testing.boundary import parse_report

pytestmark = [pytest.mark.infra, pytest.mark.timeout(1800)]

CREDENTIAL_PATTERN = re.compile(r"(?i)(token|secret|password|api_key|master_key)")
DOCKER_TIMEOUT_SECONDS = 600.0


def docker(*arguments: str) -> str:
    completed = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        timeout=DOCKER_TIMEOUT_SECONDS,
        check=False,
    )
    assert (
        completed.returncode == 0
    ), f"docker {arguments[0]} failed: {completed.stderr[-2000:]}"
    return completed.stdout


def test_the_image_carries_no_credential(worker_image: str) -> None:
    """A token in a layer would reach every worker (D68): the environment, the
    labels and every build step are scanned."""
    environment: list[str] = json.loads(
        docker("image", "inspect", "--format", "{{json .Config.Env}}", worker_image)
    )
    labels = docker(
        "image", "inspect", "--format", "{{json .Config.Labels}}", worker_image
    )
    history = docker(
        "history", "--no-trunc", "--format", "{{.CreatedBy}}", worker_image
    )
    leaks = [
        entry for entry in environment if CREDENTIAL_PATTERN.search(entry.split("=")[0])
    ] + [line for line in history.splitlines() if CREDENTIAL_PATTERN.search(line)]
    assert leaks == [], f"credential-like names in the image: {leaks}"
    assert not CREDENTIAL_PATTERN.search(labels), labels


def test_the_tools_run_as_a_non_root_user(worker_image: str) -> None:
    output = docker(
        "run",
        "--rm",
        worker_image,
        "sh",
        "-c",
        "id -u && git --version && python -m vlah.testing.scripted_worker --help "
        ">/dev/null && echo scripted-ok && claude --version && copilot --version",
    )
    lines = output.splitlines()
    assert lines[0] != "0", "the worker runs as root"
    assert "scripted-ok" in lines
    assert any(line.startswith("git version") for line in lines)


def test_the_image_holds_no_hidden_material(worker_image: str) -> None:
    """The .dockerignore allowlist keeps problem folders out of every layer (D63)."""
    report = parse_report(
        docker(
            "run",
            "--rm",
            worker_image,
            "python",
            "-m",
            "vlah.testing.scripted_worker",
            "--script",
            "probe",
            "--tool-url",
            "http://127.0.0.1:9/",
            "--search-root",
            "/",
        )
    )
    assert report["herd_yaml_found"] == []
    assert report["private_found"] == []
    assert report["credential_variables"] == []
