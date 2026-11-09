"""The worker image: no credential, no hidden material, the tools run as a non-root user (#127)."""

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from vlah.testing.boundary import parse_report

pytestmark = [pytest.mark.infra, pytest.mark.timeout(1800)]

CREDENTIAL_PATTERN = re.compile(r"(?i)(token|secret|password|api_key|master_key)")
DOCKER_TIMEOUT_SECONDS = 600.0
REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_BUILD_INPUTS = (
    "private/image-sentinel.txt",
    "vlah/private/image-sentinel.txt",
    "vlah/.env",
    "testbed/.env",
    "testbed/slow_eval/unlisted.py",
    "testbed/private/image-sentinel.txt",
    "problems/sched/reference/policy.py",
    "problems/sched/sites/holdout.yaml",
)


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


@pytest.fixture(scope="module")
def worker_image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Build a private test tag without replacing a shared developer image."""
    context = tmp_path_factory.mktemp("worker-image")
    for directory in ("vlah", "testbed", "problems"):
        shutil.copytree(
            REPO_ROOT / directory,
            context / directory,
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for filename in ("pyproject.toml", "README.md", "LICENSE", ".dockerignore"):
        shutil.copyfile(REPO_ROOT / filename, context / filename)
    for relative_path in FORBIDDEN_BUILD_INPUTS:
        destination = context / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("worker-image-private-sentinel")
    image = f"vlah-worker:package-{hashlib.sha256(str(context).encode()).hexdigest()}"
    docker(
        "build",
        "-q",
        "-t",
        image,
        "-f",
        str(REPO_ROOT / "deploy/worker/Dockerfile"),
        str(context),
    )
    try:
        yield image
    finally:
        docker("image", "rm", image)


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
    """Public build inputs must not introduce private material into the worker (D63)."""
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


def test_the_image_installs_declared_dependencies_without_operator_resources(
    worker_image: str,
) -> None:
    """The new testbed dependency must install without publishing or exposing its problems."""
    probe = """
import sys
from importlib.metadata import version
from pathlib import Path

import testbed
import vlah
from testbed.integrator import core
from testbed.problem_api import PROBLEMS_ROOT
from vlah.core.integrator import core_adapter

assert core_adapter.IntegratorCore is core.IntegratorCore
assert version("vlah") == vlah.__version__
assert version("vlah-testbed") == "0.1.0"
for module in (vlah, testbed, core):
    assert Path(module.__file__).is_relative_to(Path(sys.prefix)), module.__file__
assert not PROBLEMS_ROOT.exists(), PROBLEMS_ROOT
for path in ("/src", "/wheels", "/problems", "/opt/vlah/src"):
    assert not Path(path).exists(), path
for package in (vlah, testbed):
    root = Path(package.__file__).parent
    assert not (root / ".env").exists()
    assert not (root / "private").exists()
assert not (Path(testbed.__file__).parent / "slow_eval/unlisted.py").exists()
print("packaged-dependencies-ok")
"""
    assert docker(
        "run", "--rm", "--network", "none", worker_image, "python", "-I", "-c", probe
    ).strip() == ("packaged-dependencies-ok")
    assert "No broken requirements found" in docker(
        "run", "--rm", "--network", "none", worker_image, "python", "-m", "pip", "check"
    )
    assert docker(
        "run", "--rm", "--network", "none", worker_image, "vlah", "--version"
    ).strip() == ("vlah 0.0.1")
    output = docker(
        "run", "--rm", "--network", "none", worker_image, "vlah", "demo", "--seed", "1"
    )
    assert output.count("baseline_advanced") >= 2
    assert "1 duplicate(s) skipped" in output
    assert "1 dead end" in output
