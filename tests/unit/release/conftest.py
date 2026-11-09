"""Original release artifacts shared by script and promotion regressions."""

import sys
from pathlib import Path

import pytest

from scripts.distribution_checks import run_command
from scripts.release import prepare_bundle
from scripts.release_plan import ReleasePlan, read_projects


@pytest.fixture(scope="session")
def release_distributions(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("release-distributions") / "dist"
    run_command(
        ["uv", "build", "--all-packages", "--no-sources", "--out-dir", str(directory)],
        Path(__file__).resolve().parents[3],
    )
    return directory


@pytest.fixture(scope="session")
def release_validation(
    release_distributions: Path, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    manifest = tmp_path_factory.mktemp("release-validation") / "validation.json"
    run_command(
        [
            sys.executable,
            "-m",
            "scripts.validate_distributions",
            "--dist",
            str(release_distributions),
            "--manifest",
            str(manifest),
        ],
        Path(__file__).resolve().parents[3],
    )
    return manifest


@pytest.fixture
def release_bundle(
    release_distributions: Path, release_validation: Path, tmp_path: Path
) -> Path:
    repository_root = Path(__file__).resolve().parents[3]
    projects = read_projects(repository_root)
    plan = ReleasePlan(
        repository="minyi-tang/vlah",
        commit_sha="a" * 40,
        versions={name: project.version for name, project in projects.items()},
        tag=f"testbed-v{projects['vlah-testbed'].version}",
        package="vlah-testbed",
        publish=True,
    )
    output = tmp_path / "bundle"
    prepare_bundle(
        repository_root, release_distributions, release_validation, plan, output
    )
    return output
