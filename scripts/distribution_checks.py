"""Shared archive assertions for packaging regressions and the promoted distributions."""

import subprocess
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

COMMAND_TIMEOUT_SECONDS = 180
VLAH_PUBLIC_RESOURCES = (
    "vlah/py.typed",
    "vlah/prompts/worker/goal.md",
    "vlah/prompts/worker/rules.md",
    "vlah/prompts/worker/suggestions.md",
    "vlah/core/router/rules.default.yaml",
)
VLAH_PRIVATE_SENTINEL = b"vlah-packaging-private-material-must-not-ship"
TESTBED_PRIVATE_SENTINEL = b"packaging-regression-private-material-must-not-ship"


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
        assert len(archive.namelist()) == len(set(archive.namelist()))
        return {name: archive.read(name) for name in archive.namelist()}


def source_files(path: Path) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            assert not member.issym() and not member.islnk(), member.name
            name = Path(member.name).relative_to(path.name.removesuffix(".tar.gz"))
            assert ".." not in name.parts, member.name
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                assert name.as_posix() not in contents, member.name
                contents[name.as_posix()] = stream.read()
            else:
                assert member.isdir(), member.name
    return contents


def check_vlah_distributions(
    distributions: dict[str, Path], repository_root: Path, package_version: str
) -> None:
    direct = wheel_files(distributions["wheel"])
    assert direct == wheel_files(distributions["rebuilt"])
    for resource in VLAH_PUBLIC_RESOURCES:
        assert direct[resource] == (repository_root / resource).read_bytes()
    metadata_root = f"vlah-{package_version}.dist-info"
    metadata = BytesParser().parsebytes(direct[f"{metadata_root}/METADATA"])
    assert metadata["Name"] == "vlah"
    assert metadata["Version"] == package_version
    assert metadata["License-Expression"] == "MIT"
    assert metadata["Description-Content-Type"] == "text/markdown"
    assert (
        direct[f"{metadata_root}/licenses/LICENSE"]
        == (repository_root / "LICENSE").read_bytes()
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
    source = source_files(distributions["sdist"])
    assert {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        *VLAH_PUBLIC_RESOURCES,
    } <= source.keys()
    unexpected_source_files = {
        name
        for name in source
        if not name.startswith("vlah/")
        and name
        not in {"pyproject.toml", "README.md", "LICENSE", ".gitignore", "PKG-INFO"}
    }
    assert not unexpected_source_files
    for name, content in (direct | source).items():
        assert VLAH_PRIVATE_SENTINEL not in content, name
        assert not set(Path(name).parts) & {
            "private",
            "hidden",
            "holdout",
            "planted",
            ".herd",
            "__pycache__",
        }, name
        assert Path(name).name not in {".env", "test_hidden.py"}, name
        assert not name.endswith((".pyc", ".pyo", ".pyd", ".key")), name


def check_testbed_distributions(
    distributions: dict[str, Path], repository_root: Path, package_version: str
) -> None:
    direct = wheel_files(distributions["wheel"])
    assert direct == wheel_files(distributions["rebuilt"])
    assert "testbed/integrator/core.py" in direct
    assert "testbed/problems/sched/sim/policy_host.py" in direct
    assert "testbed/problems/stub/stub.yaml" in direct
    metadata_root = f"vlah_testbed-{package_version}.dist-info"
    metadata = BytesParser().parsebytes(direct[f"{metadata_root}/METADATA"])
    assert metadata["Name"] == "vlah-testbed"
    assert metadata["Version"] == package_version
    assert metadata["License-Expression"] == "MIT"
    assert metadata["Description-Content-Type"] == "text/markdown"
    assert (
        direct[f"{metadata_root}/licenses/LICENSE"]
        == (repository_root / "LICENSE").read_bytes()
    )
    assert all(name.startswith(("testbed/", f"{metadata_root}/")) for name in direct)
    source = source_files(distributions["sdist"])
    for name, content in (direct | source).items():
        assert TESTBED_PRIVATE_SENTINEL not in content, name
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
