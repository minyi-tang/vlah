"""Validate release identities, protected refs, and immutable artifact manifests."""

import hashlib
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import quote

from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, model_validator

PackageName = Literal["vlah", "vlah-testbed"]
VERSION_PATTERN = (
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?"
    r"(?:\.post(?:0|[1-9][0-9]*))?(?:\.dev(?:0|[1-9][0-9]*))?"
)
PackageVersion = Annotated[str, Field(pattern=f"^{VERSION_PATTERN}$")]
CommitSha = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RepositoryName = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")]
COMMAND_TIMEOUT_SECONDS = 180


class Project(BaseModel):
    model_config = ConfigDict(strict=True)
    name: PackageName
    version: PackageVersion

    def filenames(self) -> set[str]:
        basename = f"{self.name.replace('-', '_')}-{self.version}"
        return {f"{basename}-py3-none-any.whl", f"{basename}.tar.gz"}


def read_projects(repository_root: Path) -> dict[PackageName, Project]:
    projects: dict[PackageName, Project] = {}
    for name, relative_path in (
        ("vlah", "pyproject.toml"),
        ("vlah-testbed", "testbed/pyproject.toml"),
    ):
        project = Project.model_validate(
            tomllib.loads((repository_root / relative_path).read_text())["project"]
        )
        if project.name != name:
            raise ValueError(f"Unexpected project name in {relative_path}")
        else:
            projects[project.name] = project
    return projects


def parse_tag(tag: str) -> Project:
    matched = re.fullmatch(rf"(testbed-v|v)({VERSION_PATTERN})", tag)
    if matched is None:
        raise ValueError(f"Invalid release tag: {tag!r}")
    else:
        return Project(
            name="vlah-testbed" if matched[1] == "testbed-v" else "vlah",
            version=matched[2],
        )


class ReleasePlan(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    repository: RepositoryName
    commit_sha: CommitSha
    versions: dict[PackageName, PackageVersion]
    tag: str | None = None
    package: PackageName | None = None
    is_prerelease: bool = False
    publish: bool = False

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if set(self.versions) != {"vlah", "vlah-testbed"}:
            raise ValueError("Both workspace versions must be recorded")
        elif self.tag is None:
            if self.package is not None or self.publish or self.is_prerelease:
                raise ValueError("Build-only plans cannot select or publish a release")
            else:
                pass
        else:
            selected = parse_tag(self.tag)
            if (
                selected.name != self.package
                or selected.version != self.versions[selected.name]
                or self.is_prerelease != Version(selected.version).is_prerelease
            ):
                raise ValueError(
                    "Tag, package, version, or prerelease status disagrees"
                )
            else:
                pass
        return self

    def filenames(self) -> set[str]:
        return {
            filename
            for name, version in self.versions.items()
            if self.package is None or name == self.package
            for filename in Project(name=name, version=version).filenames()
        }


class ReleaseManifest(ReleasePlan):
    files: dict[str, Sha256]
    dependency_files: dict[str, Sha256] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_files(self) -> Self:
        if set(self.files) != self.filenames():
            raise ValueError("Manifest must contain exactly the selected distributions")
        elif set(self.dependency_files) != (
            Project(
                name="vlah-testbed", version=self.versions["vlah-testbed"]
            ).filenames()
            if self.package == "vlah"
            else set()
        ):
            raise ValueError("Manifest must identify the tested testbed dependency")
        else:
            return self


def artifact_hashes(directory: Path, filenames: set[str]) -> dict[str, str]:
    present = {path.name for path in directory.iterdir()}
    if present - {".gitignore", "release.json"} != filenames:
        raise ValueError(f"Unexpected or missing distributions in {directory}")
    else:
        hashes: dict[str, str] = {}
        for filename in sorted(filenames):
            path = directory / filename
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Distribution must be a regular file: {path}")
            else:
                hashes[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes


def read_bundle(directory: Path, manifest_sha256: str) -> ReleaseManifest:
    if (directory / "release.json").is_symlink():
        raise ValueError("Release manifest must not be a symlink")
    else:
        pass
    content = (directory / "release.json").read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest_sha256:
        raise ValueError("Release manifest digest differs from the build job output")
    else:
        manifest = ReleaseManifest.model_validate_json(content)
        if artifact_hashes(directory, manifest.filenames()) != manifest.files:
            raise ValueError("Distribution bytes changed after validation")
        else:
            return manifest


def command_output(command: list[str], repository_root: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if completed.returncode:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {command!r}\n"
            f"{completed.stdout}{completed.stderr}"
        )
    else:
        return completed.stdout.strip()


class RepositoryResponse(BaseModel):
    default_branch: str = Field(pattern=r"^[A-Za-z0-9._/-]+$")


class CommitResponse(BaseModel):
    sha: CommitSha


class BranchResponse(BaseModel):
    name: str
    protected: bool = Field(strict=True)
    commit: CommitResponse


class GitObject(BaseModel):
    sha: CommitSha
    type: Literal["commit", "tag"]


class GitRefResponse(BaseModel):
    object: GitObject


class ReviewerIdentity(BaseModel):
    id: int = Field(strict=True, gt=0)


class Reviewer(BaseModel):
    type: Literal["User", "Team"]
    reviewer: ReviewerIdentity


class ProtectionRule(BaseModel):
    type: str
    prevent_self_review: bool = Field(default=False, strict=True)
    reviewers: list[Reviewer] = Field(default_factory=list)


class DeploymentPolicy(BaseModel):
    protected_branches: bool = Field(strict=True)
    custom_branch_policies: bool = Field(strict=True)


class EnvironmentResponse(BaseModel):
    name: str
    protection_rules: list[ProtectionRule]
    deployment_branch_policy: DeploymentPolicy | None


class BranchPolicy(BaseModel):
    name: str
    type: Literal["tag", "branch"]


class BranchPoliciesResponse(BaseModel):
    total_count: int = Field(strict=True)
    branch_policies: list[BranchPolicy]


def check_environment(
    repository_root: Path, repository: str, environment_name: str
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", environment_name):
        raise ValueError("Invalid release environment name")
    else:
        endpoint = f"repos/{repository}/environments/{environment_name}"
        environment = EnvironmentResponse.model_validate_json(
            command_output(["gh", "api", endpoint], repository_root)
        )
        policy = environment.deployment_branch_policy
        if (
            environment.name != environment_name
            or not any(
                rule.type == "required_reviewers"
                and rule.prevent_self_review
                and rule.reviewers
                for rule in environment.protection_rules
            )
            or policy is None
            or policy.protected_branches
            or not policy.custom_branch_policies
        ):
            raise ValueError(
                "Release environment lacks required reviewer/tag protection"
            )
        else:
            policies = BranchPoliciesResponse.model_validate_json(
                command_output(
                    [
                        "gh",
                        "api",
                        f"{endpoint}/deployment-branch-policies?per_page=100",
                    ],
                    repository_root,
                )
            )
            if policies.total_count != 2 or {
                (policy.type, policy.name) for policy in policies.branch_policies
            } != {("tag", "v*"), ("tag", "testbed-v*")}:
                raise ValueError(
                    "Release environment must allow only v* and testbed-v* tags"
                )
            else:
                pass


def check_release_ref(plan: ReleasePlan, repository_root: Path) -> None:
    if plan.tag is None:
        raise ValueError("Publication requires a tag push, not a build-only plan")
    else:
        prefix = f"repos/{plan.repository}"
        repository = RepositoryResponse.model_validate_json(
            command_output(["gh", "api", prefix], repository_root)
        )
        command_output(
            ["git", "check-ref-format", "--branch", repository.default_branch],
            repository_root,
        )
        branch = BranchResponse.model_validate_json(
            command_output(
                [
                    "gh",
                    "api",
                    f"{prefix}/branches/{quote(repository.default_branch, safe='')}",
                ],
                repository_root,
            )
        )
        if not branch.protected or branch.name != repository.default_branch:
            raise ValueError("The repository default branch must be protected")
        else:
            reference = GitRefResponse.model_validate_json(
                command_output(
                    ["gh", "api", f"{prefix}/git/ref/tags/{plan.tag}"], repository_root
                )
            )
            if reference.object.type == "tag":
                reference = GitRefResponse.model_validate_json(
                    command_output(
                        ["gh", "api", f"{prefix}/git/tags/{reference.object.sha}"],
                        repository_root,
                    )
                )
            else:
                pass
            if (
                reference.object.type != "commit"
                or reference.object.sha != plan.commit_sha
            ):
                raise ValueError(
                    "The remote tag no longer identifies the workflow commit"
                )
            else:
                command_output(
                    ["git", "fetch", "--no-tags", "origin", branch.commit.sha],
                    repository_root,
                )
                command_output(
                    [
                        "git",
                        "merge-base",
                        "--is-ancestor",
                        plan.commit_sha,
                        branch.commit.sha,
                    ],
                    repository_root,
                )


def plan_release(
    repository_root: Path,
    event_name: str,
    reference: str,
    repository: str,
    commit_sha: str,
    publishing_enabled: str,
    environment_name: str,
) -> ReleasePlan:
    projects = read_projects(repository_root)
    plan = ReleasePlan(
        repository=repository,
        commit_sha=commit_sha,
        versions={name: project.version for name, project in projects.items()},
    )
    if command_output(["git", "rev-parse", "HEAD"], repository_root) != plan.commit_sha:
        raise ValueError("Checkout and workflow commit disagree")
    elif event_name in {"pull_request", "workflow_dispatch"}:
        return plan
    elif event_name != "push" or not reference.startswith("refs/tags/"):
        raise ValueError(
            "Release workflow accepts only PRs, manual builds, or tag pushes"
        )
    else:
        tag = reference.removeprefix("refs/tags/")
        selected = parse_tag(tag)
        plan = ReleasePlan(
            repository=repository,
            commit_sha=commit_sha,
            versions=plan.versions,
            tag=tag,
            package=selected.name,
            is_prerelease=Version(selected.version).is_prerelease,
            publish=publishing_enabled == "true",
        )
        check_release_ref(plan, repository_root)
        if plan.publish:
            check_environment(repository_root, repository, environment_name)
        else:
            pass
        return plan
