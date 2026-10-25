"""Docker-gated tests (marker infra): skipped, with the reason, when Docker is not running."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_IMAGE = "vlah-worker:dev"
DOCKERFILE = REPO_ROOT / "deploy" / "worker" / "Dockerfile"
DOCKER_INFO_TIMEOUT_SECONDS = 30.0
IMAGE_BUILD_TIMEOUT_SECONDS = 1800.0


def docker_unavailable_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker is not installed"
    else:
        completed = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=DOCKER_INFO_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode == 0:
            return None
        else:
            return f"docker is not running: {completed.stderr.strip()[-200:]}"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    reason = docker_unavailable_reason()
    if reason is None:
        return
    else:
        here = Path(__file__).resolve().parent
        skip = pytest.mark.skip(reason=reason)
        for item in items:
            if Path(str(item.fspath)).resolve().is_relative_to(here):
                item.add_marker(skip)
            else:
                pass


@pytest.fixture(scope="session")
def worker_image() -> str:
    """The worker image, built from the repository root; cached layers make a rebuild quick."""
    subprocess.run(
        [
            "docker",
            "build",
            "-q",
            "-t",
            WORKER_IMAGE,
            "-f",
            str(DOCKERFILE),
            str(REPO_ROOT),
        ],
        check=True,
        timeout=IMAGE_BUILD_TIMEOUT_SECONDS,
    )
    return WORKER_IMAGE
