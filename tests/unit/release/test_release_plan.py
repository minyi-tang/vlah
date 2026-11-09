"""Release authority must come from an enabled, protected, version-matched tag push."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from scripts.release import prepare_bundle
from scripts.release_plan import (
    ReleasePlan,
    artifact_hashes,
    check_environment,
    check_release_ref,
    command_output,
    parse_tag,
    read_bundle,
    read_projects,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
REPOSITORY = "minyi-tang/vlah"


@pytest.mark.parametrize(
    ("tag", "name", "version"),
    [
        ("v0.0.1", "vlah", "0.0.1"),
        ("testbed-v0.1.0", "vlah-testbed", "0.1.0"),
        ("v1.2.3rc1", "vlah", "1.2.3rc1"),
        ("testbed-v1.0.0.dev1", "vlah-testbed", "1.0.0.dev1"),
        ("v1.0.0.post1", "vlah", "1.0.0.post1"),
    ],
)
def test_independent_package_selection(tag: str, name: str, version: str) -> None:
    selected = parse_tag(tag)
    assert (selected.name, selected.version) == (name, version)


@pytest.mark.parametrize(
    "tag",
    [
        "0.0.1",
        "v0.0",
        "v00.0.1",
        "v1.0.0-rc1",
        "v1!1.0.0",
        "v1.0.0+local",
        "v1.0.0\n",
        "v1.0.0;echo unsafe",
        "v$(whoami)",
        "testbed-v1.0.0/extra",
    ],
)
def test_noncanonical_or_unsafe_tags_are_rejected(tag: str) -> None:
    with pytest.raises(ValueError, match="Invalid release tag"):
        parse_tag(tag)


@pytest.mark.parametrize(
    ("version", "is_prerelease"),
    [
        ("1.0.0", False),
        ("1.0.0.post1", False),
        ("1.0.0a1", True),
        ("1.0.0b1", True),
        ("1.0.0rc1", True),
        ("1.0.0.dev1", True),
        ("1.0.0rc1.post1", True),
    ],
)
def test_manifest_cannot_mislabel_prereleases(
    version: str, is_prerelease: bool
) -> None:
    fields = {
        "repository": REPOSITORY,
        "commit_sha": "a" * 40,
        "versions": {"vlah": version, "vlah-testbed": "0.1.0"},
        "tag": f"v{version}",
        "package": "vlah",
        "is_prerelease": is_prerelease,
    }
    assert ReleasePlan.model_validate(fields).is_prerelease == is_prerelease
    with pytest.raises(ValidationError, match="prerelease status"):
        ReleasePlan.model_validate(dict(fields, is_prerelease=not is_prerelease))


@pytest.mark.parametrize("event_name", ["pull_request", "workflow_dispatch"])
def test_actual_cli_keeps_nonpush_runs_build_only(
    tmp_path: Path, event_name: str
) -> None:
    fake_tools = tmp_path / "bin"
    fake_tools.mkdir()
    fake_github = fake_tools / "gh"
    fake_github.write_text("#!/bin/sh\nexit 99\n")
    fake_github.chmod(0o755)
    output = tmp_path / "plan.json"
    environment = dict(
        os.environ,
        GITHUB_EVENT_NAME=event_name,
        GITHUB_REF="refs/tags/v0.0.1",
        GITHUB_REPOSITORY=REPOSITORY,
        GITHUB_SHA=command_output(["git", "rev-parse", "HEAD"], REPOSITORY_ROOT),
        RELEASE_PUBLISHING_ENABLED="true",
        RELEASE_ENVIRONMENT="pypi",
        PATH=f"{fake_tools}{os.pathsep}{os.environ['PATH']}",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.release", "plan", "--output", str(output)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    plan = ReleasePlan.model_validate_json(output.read_bytes())
    assert not plan.publish and plan.tag is None and plan.package is None


@pytest.mark.parametrize("tag", ["v0.1.0", "testbed-v0.0.1"])
def test_tag_must_match_its_own_project_version(tag: str) -> None:
    with pytest.raises(ValidationError, match="version"):
        ReleasePlan(
            repository=REPOSITORY,
            commit_sha="a" * 40,
            versions={"vlah": "0.0.1", "vlah-testbed": "0.1.0"},
            tag=tag,
            package=parse_tag(tag).name,
        )


@pytest.mark.parametrize(
    "condition",
    ["ancestor", "annotated", "off-history", "unprotected", "moved", "unsafe-branch"],
)
def test_remote_gate_checks_real_git_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    command_output(["git", "init", "--initial-branch=main"], repository_root)
    command_output(["git", "config", "user.name", "Release tests"], repository_root)
    command_output(
        ["git", "config", "user.email", "release@example.invalid"], repository_root
    )
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2000-01-01T00:00:00+00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2000-01-01T00:00:00+00:00")
    tree = command_output(["git", "write-tree"], repository_root)
    ancestor = command_output(
        ["git", "commit-tree", tree, "-m", "base"], repository_root
    )
    tip = command_output(
        ["git", "commit-tree", tree, "-p", ancestor, "-m", "tip"], repository_root
    )
    orphan = command_output(
        ["git", "commit-tree", tree, "-m", "unreviewed"], repository_root
    )
    command_output(["git", "update-ref", "refs/heads/main", tip], repository_root)
    command_output(
        ["git", "remote", "add", "origin", str(repository_root)], repository_root
    )
    selected_sha = orphan if condition == "off-history" else ancestor
    responses = {
        f"repos/{REPOSITORY}": {
            "default_branch": (
                "main;echo unsafe" if condition == "unsafe-branch" else "main"
            )
        },
        f"repos/{REPOSITORY}/branches/main": {
            "name": "main",
            "protected": condition != "unprotected",
            "commit": {"sha": tip},
        },
        f"repos/{REPOSITORY}/git/ref/tags/v0.0.1": {
            "object": {
                "type": "tag" if condition == "annotated" else "commit",
                "sha": tip if condition == "moved" else selected_sha,
            },
        },
        f"repos/{REPOSITORY}/git/tags/{selected_sha}": {
            "object": {"type": "commit", "sha": selected_sha},
        },
    }

    def invoke(command: list[str], repository_root: Path) -> str:
        if command[0] == "gh":
            return json.dumps(responses[command[-1]])
        else:
            return command_output(command, repository_root)

    monkeypatch.setattr("scripts.release_plan.command_output", invoke)
    plan = ReleasePlan(
        repository=REPOSITORY,
        commit_sha=selected_sha,
        versions={"vlah": "0.0.1", "vlah-testbed": "0.1.0"},
        tag="v0.0.1",
        package="vlah",
        publish=True,
    )
    if condition in {"ancestor", "annotated"}:
        check_release_ref(plan, repository_root)
    else:
        with pytest.raises((ValueError, RuntimeError)):
            check_release_ref(plan, repository_root)


@pytest.mark.parametrize(
    "condition",
    [
        "protected",
        "no-reviewers",
        "self-review",
        "all-branches",
        "extra-branch",
        "extra-tag",
        "truncated",
    ],
)
def test_environment_must_exist_and_require_reviewed_tags(
    monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    environment = {
        "name": "pypi",
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": condition != "self-review",
                "reviewers": (
                    []
                    if condition == "no-reviewers"
                    else [
                        {"type": "User", "reviewer": {"id": 1}},
                    ]
                ),
            }
        ],
        "deployment_branch_policy": (
            None
            if condition == "all-branches"
            else {
                "protected_branches": False,
                "custom_branch_policies": True,
            }
        ),
    }
    policies = [{"type": "tag", "name": "v*"}, {"type": "tag", "name": "testbed-v*"}]
    if condition in {"extra-branch", "extra-tag"}:
        policies.append(
            {"type": "branch" if condition == "extra-branch" else "tag", "name": "*"}
        )
    else:
        pass

    def invoke(command: list[str], repository_root: Path) -> str:
        assert repository_root == REPOSITORY_ROOT
        if command[-1].endswith("per_page=100"):
            return json.dumps(
                {
                    "total_count": 101 if condition == "truncated" else len(policies),
                    "branch_policies": policies,
                }
            )
        else:
            return json.dumps(environment)

    monkeypatch.setattr("scripts.release_plan.command_output", invoke)
    if condition == "protected":
        check_environment(REPOSITORY_ROOT, REPOSITORY, "pypi")
    else:
        with pytest.raises(ValueError, match="environment"):
            check_environment(REPOSITORY_ROOT, REPOSITORY, "pypi")


@pytest.mark.parametrize("package", ["vlah", "vlah-testbed", None])
def test_bundle_promotes_only_selected_unchanged_bytes(
    release_distributions: Path,
    tmp_path: Path,
    package: Literal["vlah", "vlah-testbed"] | None,
) -> None:
    projects = read_projects(REPOSITORY_ROOT)
    hashes = artifact_hashes(
        release_distributions,
        {filename for project in projects.values() for filename in project.filenames()},
    )
    validation = tmp_path / "validation.json"
    validation.write_text(json.dumps(hashes))
    plan = ReleasePlan(
        repository=REPOSITORY,
        commit_sha="a" * 40,
        versions={name: project.version for name, project in projects.items()},
        tag=(
            None
            if package is None
            else ("v0.0.1" if package == "vlah" else "testbed-v0.1.0")
        ),
        package=package,
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    manifest = prepare_bundle(
        REPOSITORY_ROOT, release_distributions, validation, plan, first
    )
    prepare_bundle(REPOSITORY_ROOT, release_distributions, validation, plan, second)
    assert (first / "release.json").read_bytes() == (
        second / "release.json"
    ).read_bytes()
    assert set(manifest.files) == plan.filenames()
    assert len(manifest.files) == (4 if package is None else 2)
    for filename, digest in manifest.files.items():
        assert (first / filename).read_bytes() == (
            release_distributions / filename
        ).read_bytes()
        assert digest == hashes[filename]
    manifest_digest = hashlib.sha256((first / "release.json").read_bytes()).hexdigest()
    assert read_bundle(first, manifest_digest) == manifest
    changed = first / sorted(manifest.files)[0]
    changed.write_bytes(changed.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="bytes changed"):
        read_bundle(first, manifest_digest)
    with pytest.raises(ValueError, match="manifest digest"):
        read_bundle(second, "0" * 64)


def test_changed_build_output_cannot_reuse_validation(
    release_distributions: Path, tmp_path: Path
) -> None:
    directory = tmp_path / "dist"
    shutil.copytree(release_distributions, directory)
    projects = read_projects(REPOSITORY_ROOT)
    filenames = {
        filename for project in projects.values() for filename in project.filenames()
    }
    validation = tmp_path / "validation.json"
    validation.write_text(json.dumps(artifact_hashes(directory, filenames)))
    next(directory.glob("*.whl")).write_bytes(b"changed after validation")
    plan = ReleasePlan(
        repository=REPOSITORY,
        commit_sha="a" * 40,
        versions={name: project.version for name, project in projects.items()},
    )
    with pytest.raises(ValueError, match="validation report"):
        prepare_bundle(
            REPOSITORY_ROOT, directory, validation, plan, tmp_path / "bundle"
        )
    assert not (tmp_path / "bundle").exists()
