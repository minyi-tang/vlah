"""Regressions for empty wheels, checkout-dependent archives, and private build inputs."""

import json
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

from scripts.distribution_checks import (
    COMMAND_TIMEOUT_SECONDS,
    TESTBED_PRIVATE_SENTINEL,
    check_testbed_distributions,
    run_command,
)
from scripts.installed_checks import (
    InstalledTestbed,
    check_installed_testbed,
    installed_testbed,
    runtime_environment,
)
from testbed.problem_api import REPO_ROOT, copy_writable_file

pytestmark = pytest.mark.timeout(300)
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
        destination.write_bytes(TESTBED_PRIVATE_SENTINEL)
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
    check_testbed_distributions(distributions, REPO_ROOT, "0.1.0")


@pytest.fixture(scope="module", params=["wheel", "rebuilt"])
def installed(
    request: pytest.FixtureRequest,
    distributions: dict[str, Path],
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[InstalledTestbed]:
    workdir = tmp_path_factory.mktemp(f"installed-{request.param}")
    with installed_testbed(distributions[request.param], workdir, REPO_ROOT) as result:
        yield result


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
    check_installed_testbed(installed, check, REPO_ROOT)


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
