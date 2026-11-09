"""Regressions for empty wheels, checkout-dependent archives, and private build inputs."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import Literal

import pytest

from testbed.problem_api import REPO_ROOT, copy_writable_file

pytestmark = pytest.mark.timeout(300)
COMMAND_TIMEOUT_SECONDS = 180
PRIVATE_SENTINEL = b"packaging-regression-private-material-must-not-ship"
FORBIDDEN_INPUTS = (
    "private/testbed/sched/reference/policy.py",
    "testbed/DECISIONS.md",
    "testbed/.env",
    "testbed/private/system.key",
    "testbed/slow_eval/credentials.py",
    "testbed/var/runs/events.jsonl",
    "testbed/transcripts/session.jsonl",
    "testbed/extras/alt_policies/answer.py",
    "problems/sched/hidden/seeds.yaml",
    "problems/sched/reference/policy.py",
    "problems/sched/probes/__init__.py",
    "problems/sched/planted/policy.py",
    "problems/sched/problem.yaml",
    "problems/sched/sites/holdout.yaml",
    "problems/stub/tasks/holdout/case.json",
)


@dataclass(frozen=True)
class InstalledTestbed:
    workdir: Path
    python_executable: Path
    environment: dict[str, str]


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


def runtime_environment(home: Path) -> dict[str, str]:
    home.mkdir(parents=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("TB_")
        and name not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    return dict(environment, HOME=str(home), TB_PRIVATE="none", TB_WORKERS="2")


def wheel_files(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


@pytest.fixture(scope="module")
def source_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    source = tmp_path_factory.mktemp("testbed-source") / "project"
    for directory in ("testbed", "problems"):
        shutil.copytree(
            REPO_ROOT / directory,
            source / directory,
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for relative_path in FORBIDDEN_INPUTS:
        destination = source / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(PRIVATE_SENTINEL)
    return source


@pytest.fixture(scope="module")
def distributions(
    source_tree: Path, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Path]:
    workdir = tmp_path_factory.mktemp("testbed-distributions")
    output = workdir / "dist"
    run_command(
        [
            "uv",
            "build",
            "--no-sources",
            "--sdist",
            "--wheel",
            str(source_tree / "testbed"),
            "--out-dir",
            str(output),
        ],
        workdir,
    )
    source_archive = next(output.glob("*.tar.gz"))
    with tarfile.open(source_archive) as archive:
        archive.extractall(workdir / "extracted", filter="data")
    extracted = workdir / "extracted" / source_archive.name.removesuffix(".tar.gz")
    unavailable_source = source_tree.with_name("unavailable-project")
    source_tree.rename(unavailable_source)
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
        unavailable_source.rename(source_tree)
    return {
        "wheel": next(output.glob("*.whl")),
        "sdist": source_archive,
        "rebuilt": next((workdir / "rebuilt").glob("*.whl")),
    }


def test_distributions_are_public_and_rebuild_equivalently(
    distributions: dict[str, Path],
) -> None:
    """A successful build used to ship no code in the wheel and operator policies in the sdist."""
    direct = wheel_files(distributions["wheel"])
    assert direct == wheel_files(distributions["rebuilt"])
    assert "testbed/integrator/core.py" in direct
    assert "testbed/problems/sched/sim/policy_host.py" in direct
    assert "testbed/problems/stub/stub.yaml" in direct
    metadata_root = "vlah_testbed-0.1.0.dist-info"
    metadata = BytesParser().parsebytes(direct[f"{metadata_root}/METADATA"])
    assert metadata["Name"] == "vlah-testbed"
    assert metadata["Version"] == "0.1.0"
    assert metadata["License-Expression"] == "MIT"
    assert metadata["Description-Content-Type"] == "text/markdown"
    assert (
        direct[f"{metadata_root}/licenses/LICENSE"]
        == (REPO_ROOT / "LICENSE").read_bytes()
    )
    assert all(name.startswith(("testbed/", f"{metadata_root}/")) for name in direct)
    with tarfile.open(distributions["sdist"]) as archive:
        source_files: dict[str, bytes] = {}
        for member in archive.getmembers():
            assert not member.issym() and not member.islnk()
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                source_files[member.name] = stream.read()
            else:
                pass
    for name, content in (direct | source_files).items():
        assert PRIVATE_SENTINEL not in content, name
        assert not set(Path(name).parts) & {
            "private",
            "hidden",
            "holdout",
            "planted",
            "reference",
            "probes",
            "extras",
            "var",
            "transcripts",
            "__pycache__",
        }, name
        assert Path(name).name not in {"DECISIONS.md", "system.key", ".env"}, name
        assert not name.endswith(".pyc"), name


@pytest.fixture(scope="module", params=["wheel", "rebuilt"])
def installed(
    request: pytest.FixtureRequest,
    distributions: dict[str, Path],
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[InstalledTestbed]:
    workdir = tmp_path_factory.mktemp(f"installed-{request.param}")
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
            str(distributions[request.param]),
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
    assert not package_root.is_relative_to(REPO_ROOT)
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


@pytest.mark.parametrize(
    "check",
    [
        "cli",
        "paths",
        "herd",
        pytest.param(
            "sandbox",
            marks=pytest.mark.skipif(
                sys.platform != "darwin" or not shutil.which("sandbox-exec"),
                reason="macOS sandbox",
            ),
        ),
    ],
)
def test_read_only_installed_package(
    installed: InstalledTestbed, check: Literal["cli", "paths", "herd", "sandbox"]
) -> None:
    """Installed imports, resources, writable workspaces and sandbox boundaries must all work."""
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
            str(Path(__file__).with_name("packaging_checks.py")),
            check,
        ],
        installed.workdir,
        environment,
    )


def test_editable_install_preserves_checkout_paths(tmp_path: Path) -> None:
    virtualenv = tmp_path / "editable-environment"
    run_command(["uv", "venv", "--python", sys.executable, str(virtualenv)], tmp_path)
    python = str(virtualenv / "bin/python")
    run_command(
        [
            "uv",
            "pip",
            "install",
            "--python",
            python,
            "--no-deps",
            "-e",
            str(REPO_ROOT / "testbed"),
        ],
        tmp_path,
    )
    environment = runtime_environment(tmp_path / "home")
    environment.pop("TB_PRIVATE")
    probe = run_command(
        [
            python,
            "-I",
            "-c",
            "import json; import testbed; "
            "from testbed.problem_api import PROBLEMS_ROOT, STATE_DIR, private_dirs; "
            "print(json.dumps([testbed.__file__, str(PROBLEMS_ROOT), str(STATE_DIR), "
            "[str(path) for path in private_dirs()]]))",
        ],
        tmp_path,
        environment,
    )
    assert json.loads(probe.stdout) == [
        str(REPO_ROOT / "testbed/__init__.py"),
        str(REPO_ROOT / "problems"),
        str(REPO_ROOT / "private/testbed/var"),
        [
            str(REPO_ROOT / "private/testbed"),
            str(Path(environment["HOME"]) / ".vlah-private/testbed"),
        ],
    ]


@pytest.mark.parametrize(
    "relative_path", ["testbed/problem_api.py", "problems/sched/sites/common.yaml"]
)
@pytest.mark.parametrize("is_symlink", [False, True], ids=["missing", "symlink"])
def test_missing_or_redirected_public_inputs_fail_the_build(
    source_tree: Path, tmp_path: Path, relative_path: str, is_symlink: bool
) -> None:
    source = tmp_path / "project"
    shutil.copytree(source_tree, source, symlinks=True)
    selected = source / relative_path
    selected.unlink()
    if is_symlink:
        selected.symlink_to(source / "private/testbed/sched/reference/policy.py")
    else:
        pass
    result = subprocess.run(
        [
            "uv",
            "build",
            "--no-sources",
            "--wheel",
            str(source / "testbed"),
            "--out-dir",
            str(tmp_path / "dist"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode != 0
    assert (
        "distribution input must not be a symlink"
        if is_symlink
        else "required distribution input is missing"
    ) in result.stderr


@pytest.mark.parametrize("mode", [0o444, 0o555, 0o640])
def test_template_copies_preserve_source_and_executable_bits(
    tmp_path: Path, mode: int
) -> None:
    source = tmp_path / "template"
    source.write_bytes(b"public template")
    source.chmod(mode)
    destination = tmp_path / "workspace-copy"
    copy_writable_file(source, destination)
    assert stat.S_IMODE(source.stat().st_mode) == mode
    assert stat.S_IMODE(destination.stat().st_mode) == mode | stat.S_IWUSR
    assert destination.read_bytes() == source.read_bytes() == b"public template"
    destination.write_bytes(b"modified candidate")
    assert source.read_bytes() == b"public template"
