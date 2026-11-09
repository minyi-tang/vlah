"""Regressions for undeclared runtime dependencies and checkout-dependent distributions."""

import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from importlib.metadata import version
from pathlib import Path
from typing import Literal

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

import vlah
from vlah.cli import main

pytestmark = pytest.mark.timeout(300)
REPO_ROOT = Path(__file__).resolve().parents[1]
COMMAND_TIMEOUT_SECONDS = 180
PUBLIC_RESOURCES = (
    "vlah/py.typed",
    "vlah/prompts/worker/goal.md",
    "vlah/prompts/worker/rules.md",
    "vlah/prompts/worker/suggestions.md",
    "vlah/core/router/rules.default.yaml",
)
PRIVATE_SENTINEL = b"vlah-packaging-private-material-must-not-ship"
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
INSTALLED_PROBE = """
import sys
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

import testbed
import vlah
from testbed.integrator import core
from vlah.core.integrator import core_adapter
from vlah.core.router.rules import default_rules
from vlah.core.types.tasks import Task
from vlah.prompts.worker import render_worker_prompt

for module in (vlah, testbed, core, core_adapter):
    assert Path(module.__file__).is_relative_to(Path(sys.prefix)), module.__file__
assert core_adapter.IntegratorCore is core.IntegratorCore
assert version("vlah") == vlah.__version__
assert version("vlah-testbed") == sys.argv[1]
assert files("vlah").joinpath("py.typed").is_file()
assert default_rules()
task = Task(
    id="packaged-task",
    approach="packaged-approach",
    kind="advance",
    goal="Exercise packaged prompts",
    created_by="human",
)
prompt = render_worker_prompt("worker-01", task)
assert "Your goal: Exercise packaged prompts" in prompt
assert "worker-01" in prompt
assert "# Rules the herd enforces" in prompt
assert "# Suggestions" in prompt
print(vlah.__version__)
"""


def run_command(
    command: list[str], cwd: Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed


def wheel_files(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


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
        destination.write_bytes(PRIVATE_SENTINEL)
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
    direct = wheel_files(distributions["wheel"])
    assert direct == wheel_files(distributions["rebuilt"])
    for resource in PUBLIC_RESOURCES:
        assert direct[resource] == (REPO_ROOT / resource).read_bytes()
    metadata_root = f"vlah-{vlah.__version__}.dist-info"
    metadata = BytesParser().parsebytes(direct[f"{metadata_root}/METADATA"])
    assert metadata["Name"] == "vlah"
    assert metadata["Version"] == vlah.__version__
    assert metadata["License-Expression"] == "MIT"
    assert metadata["Description-Content-Type"] == "text/markdown"
    assert (
        direct[f"{metadata_root}/licenses/LICENSE"]
        == (REPO_ROOT / "LICENSE").read_bytes()
    )
    requirements = [
        Requirement(requirement)
        for requirement in metadata.get_all("Requires-Dist", [])
    ]
    assert all(requirement.url is None for requirement in requirements)
    testbed = next(
        requirement
        for requirement in requirements
        if requirement.name == "vlah-testbed"
    )
    assert testbed.specifier == SpecifierSet(">=0.1.0,<0.2.0")
    assert testbed.marker is None and not testbed.extras
    assert all(name.startswith(("vlah/", f"{metadata_root}/")) for name in direct)
    source_files: dict[str, bytes] = {}
    with tarfile.open(distributions["sdist"]) as archive:
        for member in archive.getmembers():
            assert not member.issym() and not member.islnk()
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                name = Path(member.name).relative_to(f"vlah-{vlah.__version__}")
                source_files[name.as_posix()] = stream.read()
            else:
                pass
    assert {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        *PUBLIC_RESOURCES,
    } <= source_files.keys()
    unexpected_source_files = {
        name
        for name in source_files
        if not name.startswith("vlah/")
        and name
        not in {"pyproject.toml", "README.md", "LICENSE", ".gitignore", "PKG-INFO"}
    }
    assert not unexpected_source_files
    for name, content in (direct | source_files).items():
        assert PRIVATE_SENTINEL not in content, name
        assert "__pycache__" not in Path(name).parts, name
        assert not name.endswith(".pyc"), name


@pytest.mark.parametrize("artifact", ["wheel", "sdist", "rebuilt"])
def test_installed_cli_outside_checkout(
    distributions: dict[str, Path],
    tmp_path: Path,
    artifact: Literal["wheel", "sdist", "rebuilt"],
) -> None:
    """Installing only VLAH used to fail before CLI startup because testbed was undeclared."""
    assert not tmp_path.is_relative_to(REPO_ROOT)
    virtualenv = tmp_path / "environment"
    run_command(["uv", "venv", "--python", sys.executable, str(virtualenv)], tmp_path)
    python_executable = virtualenv / "bin" / "python"
    constraint = tmp_path / "constraints.txt"
    constraint.write_text(f"vlah-testbed @ {distributions['testbed'].as_uri()}\n")
    run_command(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python_executable),
            "--find-links",
            str(distributions["testbed"].parent),
            "--constraint",
            str(constraint),
            str(distributions[artifact]),
        ],
        tmp_path,
    )
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
        and not name.startswith("TB_")
    }
    environment.update(HOME=str(home), TB_PRIVATE="none")
    testbed_project = tomllib.loads((REPO_ROOT / "testbed/pyproject.toml").read_text())[
        "project"
    ]
    probe = run_command(
        [
            str(python_executable),
            "-I",
            "-c",
            INSTALLED_PROBE,
            testbed_project["version"],
        ],
        tmp_path,
        environment,
    )
    assert probe.stdout == f"{vlah.__version__}\n"
    command = [str(python_executable), "-I", str(virtualenv / "bin" / "vlah")]
    assert run_command(command + ["--version"], tmp_path, environment).stdout == (
        f"vlah {vlah.__version__}\n"
    )
    help_text = run_command(command + ["--help"], tmp_path, environment).stdout
    assert "usage: vlah" in help_text and "demo" in help_text
    demo_command = command + ["demo", "--seed", "1"]
    first = run_command(demo_command, tmp_path, environment).stdout
    assert first == run_command(demo_command, tmp_path, environment).stdout
    assert first.count("baseline_advanced") >= 2
    for marker in (
        "dedup_hit",
        "escalation",
        "stopped",
        "baseline 100 ->",
        "1 duplicate(s) skipped",
        "1 dead end",
    ):
        assert marker in first
