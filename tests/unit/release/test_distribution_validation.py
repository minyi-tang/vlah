"""The release CLI must validate the original files, not unrelated fixture builds."""

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.release_plan import artifact_hashes, read_projects

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.timeout(300)


def test_actual_distribution_cli_records_the_original_bytes(
    release_distributions: Path, release_validation: Path
) -> None:
    projects = read_projects(REPOSITORY_ROOT)
    filenames = {
        filename for project in projects.values() for filename in project.filenames()
    }
    assert json.loads(release_validation.read_text()) == artifact_hashes(
        release_distributions, filenames
    )
    assert len(filenames) == 4


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "symlink", "corrupt-source", "wrong-version", "private-file"],
)
def test_actual_validator_rejects_invalid_builds_without_a_report(
    release_distributions: Path, tmp_path: Path, mutation: str
) -> None:
    directory = tmp_path / "dist"
    shutil.copytree(release_distributions, directory)
    wheel = next(directory.glob("vlah-*.whl"))
    if mutation == "missing":
        wheel.unlink()
    elif mutation == "extra":
        (directory / "old-version.whl").write_bytes(b"obsolete build")
    elif mutation == "symlink":
        wheel.unlink()
        wheel.symlink_to(release_distributions / wheel.name)
    elif mutation == "corrupt-source":
        next(directory.glob("vlah-*.tar.gz")).write_bytes(b"not a source archive")
    elif mutation == "wrong-version":
        replacement = directory / "replacement.zip"
        with (
            zipfile.ZipFile(wheel) as original,
            zipfile.ZipFile(replacement, "w") as changed,
        ):
            for name in original.namelist():
                content = original.read(name)
                if name.endswith("/METADATA"):
                    content = content.replace(b"Version: 0.0.1", b"Version: 9.9.9")
                else:
                    pass
                changed.writestr(name, content)
        replacement.replace(wheel)
    else:
        with zipfile.ZipFile(wheel, "a") as changed:
            changed.writestr("vlah/private/credentials.py", b"not-a-real-secret")
    report = tmp_path / "validation.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.validate_distributions",
            "--dist",
            str(directory),
            "--manifest",
            str(report),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode != 0, completed.stdout
    assert not report.exists()
    assert "Traceback" in completed.stderr


@pytest.mark.parametrize("command", ["publish-pypi", "publish-github"])
def test_actual_publish_cli_rejects_dispatch_even_with_valid_bundle(
    release_bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    from_manifest = release_bundle / "release.json"
    digest = hashlib.sha256(from_manifest.read_bytes()).hexdigest()
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/testbed-v0.1.0")
    monkeypatch.setenv("GITHUB_REPOSITORY", "minyi-tang/vlah")
    current_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT, text=True
    ).strip()
    monkeypatch.setenv("GITHUB_SHA", current_sha)
    monkeypatch.setenv("RELEASE_PUBLISHING_ENABLED", "true")
    monkeypatch.setenv("RELEASE_ENVIRONMENT", "pypi")
    fake_github = tmp_path / "gh"
    fake_github.write_text("#!/bin/sh\nexit 99\n")
    fake_github.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{sys.prefix}/bin:/usr/bin:/bin")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.release",
            command,
            "--bundle",
            str(release_bundle),
            "--manifest-sha256",
            digest,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode != 0
    assert (
        "Publishing requires the same enabled, protected tag-push plan"
        in completed.stderr
    )
