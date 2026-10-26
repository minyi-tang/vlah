"""Regressions for undeclared runtime dependencies and checkout-dependent distributions."""

import shutil
import tarfile
import tomllib
from importlib.metadata import version
from pathlib import Path
from typing import Literal

import pytest

import vlah
from scripts.distribution_checks import (
    VLAH_PRIVATE_SENTINEL,
    check_vlah_distributions,
    run_command,
)
from scripts.installed_checks import check_installed_vlah
from vlah.cli import main

pytestmark = pytest.mark.timeout(300)
REPO_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_INPUTS = (
    "private/testbed/hidden.txt",
    "vlah/private/credentials.py",
    "vlah/hidden/score.py",
    "vlah/holdout/score.py",
    "vlah/planted/solution.py",
    "vlah/.herd/events.jsonl",
    "vlah/__pycache__/cached.pyc",
    "vlah/test_hidden.py",
    "vlah/.env",
    "vlah/operator.key",
    "docs/unrelated.md",
    "examples/unrelated.txt",
    "tests/test_unrelated.py",
    "ui/.env",
    ".github/workflows/unrelated.yaml",
)


def test_version_matches_metadata(capsys: pytest.CaptureFixture[str]) -> None:
    """The Python attribute and CLI must not drift from the distribution's version."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    assert vlah.__version__ == version("vlah") == project["version"]
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == f"vlah {project['version']}\n"


@pytest.fixture(scope="module")
def distributions(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    workdir = tmp_path_factory.mktemp("vlah-distributions")
    source = workdir / "project"
    for directory in ("vlah", "testbed", "problems"):
        shutil.copytree(
            REPO_ROOT / directory,
            source / directory,
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for filename in ("pyproject.toml", "README.md", "LICENSE", ".gitignore"):
        shutil.copyfile(REPO_ROOT / filename, source / filename)
    for relative_path in FORBIDDEN_INPUTS:
        destination = source / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(VLAH_PRIVATE_SENTINEL)
    output = workdir / "dist"
    run_command(
        [
            "uv",
            "build",
            "--all-packages",
            "--no-sources",
            "--sdist",
            "--wheel",
            "--out-dir",
            str(output),
        ],
        source,
    )
    source_archive = next(output.glob("vlah-*.tar.gz"))
    with tarfile.open(source_archive) as archive:
        archive.extractall(workdir / "extracted", filter="data")
    extracted = workdir / "extracted" / source_archive.name.removesuffix(".tar.gz")
    unavailable_source = source.with_name("unavailable-project")
    source.rename(unavailable_source)
    try:
        run_command(
            [
                "uv",
                "build",
                "--no-sources",
                "--wheel",
                str(extracted),
                "--out-dir",
                str(workdir / "rebuilt"),
            ],
            workdir,
        )
    finally:
        unavailable_source.rename(source)
    return {
        "wheel": next(output.glob("vlah-*.whl")),
        "sdist": source_archive,
        "rebuilt": next((workdir / "rebuilt").glob("vlah-*.whl")),
        "testbed": next(output.glob("vlah_testbed-*.whl")),
    }


def test_distributions_declare_dependencies_and_rebuild_equivalently(
    distributions: dict[str, Path],
) -> None:
    """The wheel omitted testbed, and the sdist bundled unrelated repository trees."""
    check_vlah_distributions(distributions, REPO_ROOT, vlah.__version__)


@pytest.mark.parametrize("artifact", ["wheel", "sdist", "rebuilt"])
def test_installed_cli_outside_checkout(
    distributions: dict[str, Path],
    tmp_path: Path,
    artifact: Literal["wheel", "sdist", "rebuilt"],
) -> None:
    """Installing only VLAH used to fail before CLI startup because testbed was undeclared."""
    check_installed_vlah(
        distributions[artifact], distributions["testbed"], tmp_path, REPO_ROOT
    )
