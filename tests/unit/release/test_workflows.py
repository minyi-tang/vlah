"""Workflow permissions and event guards must survive release-tool refactors."""

import re
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_release_workflow_privileges_and_artifact_flow_are_fail_closed() -> None:
    workflow = yaml.load(
        (REPOSITORY_ROOT / ".github/workflows/release.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"push", "workflow_dispatch"}
    assert workflow["on"]["push"]["tags"] == ["v*", "testbed-v*"]
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["pypi"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert jobs["github"]["permissions"] == {"contents": "write"}
    assert jobs["pypi"]["needs"] == "build"
    assert jobs["github"]["needs"] == ["build", "pypi"]
    for name in ("pypi", "github"):
        assert jobs[name]["environment"]["name"] == "pypi"
        clauses = {clause.strip() for clause in jobs[name]["if"].split("&&")}
        assert clauses == {
            "github.repository == 'minyi-tang/vlah'",
            "github.event_name == 'push'",
            "github.ref_type == 'tag'",
            "vars.PYPI_PUBLISH_ENABLED == 'true'",
            "needs.build.outputs.publish == 'true'",
        } | ({"needs.pypi.result == 'success'"} if name == "github" else set())
        download = next(
            step
            for step in jobs[name]["steps"]
            if "download-artifact@" in step.get("uses", "")
        )
        assert (
            download["with"]["artifact-ids"] == "${{ needs.build.outputs.artifact-id }}"
        )
        assert "overwrite" not in download["with"]
        run = "\n".join(step.get("run", "") for step in jobs[name]["steps"])
        assert "uv build" not in run and "uv sync" not in run
        assert '--manifest-sha256 "$MANIFEST_SHA256"' in run
    for job in jobs.values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
            else:
                pass


def test_pr_distribution_job_is_read_only_and_always_uses_dry_run() -> None:
    ci = yaml.load(
        (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert "pull_request" in ci["on"]
    assert ci["permissions"] == {"contents": "read"}
    assert ci["jobs"]["distributions"]["permissions"] == {"contents": "read"}
    commands = "\n".join(
        step.get("run", "") for step in ci["jobs"]["distributions"]["steps"]
    )
    assert commands.count("uv build --all-packages --no-sources") == 1
    assert "scripts.validate_distributions" in commands
    assert "uv publish --dry-run --trusted-publishing never" in commands
