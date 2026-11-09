"""Shared outside-checkout probes for original and source-rebuilt distributions."""

import os
import stat
import sys
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from scripts.distribution_checks import run_command

VLAH_INSTALLED_PROBE = """
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


@dataclass(frozen=True)
class InstalledTestbed:
    workdir: Path
    python_executable: Path
    environment: dict[str, str]


def runtime_environment(home: Path) -> dict[str, str]:
    home.mkdir(parents=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("TB_")
        and name not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    return dict(environment, HOME=str(home), TB_PRIVATE="none", TB_WORKERS="2")


@contextmanager
def installed_testbed(
    distribution: Path, workdir: Path, repository_root: Path
) -> Iterator[InstalledTestbed]:
    virtualenv = workdir / "environment"
    run_command(["uv", "venv", "--python", sys.executable, str(virtualenv)], workdir)
    python_executable = virtualenv / "bin" / "python"
    run_command(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python_executable),
            str(distribution),
        ],
        workdir,
    )
    environment = runtime_environment(workdir / "home")
    imported = run_command(
        [str(python_executable), "-I", "-c", "import testbed; print(testbed.__file__)"],
        workdir,
        environment,
    )
    package_root = Path(imported.stdout.strip()).parent
    assert package_root.is_relative_to(virtualenv)
    assert not package_root.is_relative_to(repository_root)
    permissions = {
        path: stat.S_IMODE(path.stat().st_mode)
        for path in [package_root, *package_root.rglob("*")]
    }
    for path in permissions:
        path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        yield InstalledTestbed(workdir, python_executable, environment)
    finally:
        for path, mode in permissions.items():
            path.chmod(mode)


def check_installed_testbed(
    installed: InstalledTestbed,
    check: Literal["cli", "paths", "herd", "sandbox"],
    repository_root: Path,
) -> None:
    environment = dict(installed.environment)
    if check in ("paths", "sandbox"):
        environment.update(
            TB_STATE_DIR=str(installed.workdir / "environment/operator-state"),
            TB_PRIVATE=str(installed.workdir / "environment/operator-private"),
        )
    else:
        pass
    run_command(
        [
            str(installed.python_executable),
            "-I",
            str(repository_root / "tests/testbed/packaging_checks.py"),
            check,
        ],
        installed.workdir,
        environment,
    )


def check_installed_vlah(
    distribution: Path, testbed_wheel: Path, workdir: Path, repository_root: Path
) -> None:
    assert not workdir.is_relative_to(repository_root)
    virtualenv = workdir / "environment"
    run_command(["uv", "venv", "--python", sys.executable, str(virtualenv)], workdir)
    python_executable = virtualenv / "bin" / "python"
    constraint = workdir / "constraints.txt"
    constraint.write_text(f"vlah-testbed @ {testbed_wheel.as_uri()}\n")
    run_command(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python_executable),
            "--find-links",
            str(testbed_wheel.parent),
            "--constraint",
            str(constraint),
            str(distribution),
        ],
        workdir,
    )
    home = workdir / "home"
    home.mkdir()
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
        and not name.startswith("TB_")
    }
    environment.update(HOME=str(home), TB_PRIVATE="none")
    project = tomllib.loads((repository_root / "pyproject.toml").read_text())["project"]
    testbed_project = tomllib.loads(
        (repository_root / "testbed/pyproject.toml").read_text()
    )["project"]
    probe = run_command(
        [
            str(python_executable),
            "-I",
            "-c",
            VLAH_INSTALLED_PROBE,
            testbed_project["version"],
        ],
        workdir,
        environment,
    )
    assert probe.stdout == f"{project['version']}\n"
    command = [str(python_executable), "-I", str(virtualenv / "bin" / "vlah")]
    assert run_command(command + ["--version"], workdir, environment).stdout == (
        f"vlah {project['version']}\n"
    )
    help_text = run_command(command + ["--help"], workdir, environment).stdout
    assert "usage: vlah" in help_text and "demo" in help_text
    demo_command = command + ["demo", "--seed", "1"]
    first = run_command(demo_command, workdir, environment).stdout
    assert first == run_command(demo_command, workdir, environment).stdout
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
