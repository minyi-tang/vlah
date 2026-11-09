"""Promote immutable bundles with digest-checked PyPI and GitHub retry behavior."""

import hashlib
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen

from pydantic import BaseModel, Field, TypeAdapter

from scripts.release_plan import (
    COMMAND_TIMEOUT_SECONDS,
    Project,
    ReleaseManifest,
    Sha256,
    command_output,
)

HTTP_TIMEOUT_SECONDS = 30


def registry_url(base_url: str, suffix: str = "") -> str:
    parsed = urlsplit(base_url)
    if (
        (
            parsed.scheme != "https"
            and not (parsed.scheme == "http" and parsed.hostname == "127.0.0.1")
        )
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Registry URLs require HTTPS (HTTP is only for loopback tests)"
        )
    else:
        return f"{base_url.rstrip('/')}/{suffix}"


class RegistryDigests(BaseModel):
    sha256: Sha256


class RegistryFile(BaseModel):
    filename: str
    digests: RegistryDigests
    yanked: bool = Field(strict=True)


class RegistryRelease(BaseModel):
    info: Project
    urls: list[RegistryFile]


def check_registry(
    project: Project,
    expected_files: dict[str, str],
    json_url: str,
    require_complete: bool,
) -> None:
    endpoint = registry_url(json_url, f"{project.name}/{project.version}/json")
    try:
        with urlopen(endpoint, timeout=HTTP_TIMEOUT_SECONDS) as response:
            release = RegistryRelease.model_validate_json(response.read())
    except HTTPError as error:
        if error.code == 404 and not require_complete:
            return
        else:
            raise
    if release.info != project:
        raise ValueError("Registry response identifies a different package or version")
    else:
        published: dict[str, str] = {}
        for artifact in release.urls:
            if artifact.yanked or artifact.filename in published:
                raise ValueError("Registry version has yanked or duplicate files")
            elif expected_files.get(artifact.filename) != artifact.digests.sha256:
                raise ValueError(
                    f"Registry file differs from validated bytes: {artifact.filename}"
                )
            else:
                published[artifact.filename] = artifact.digests.sha256
        if require_complete and published != expected_files:
            raise ValueError(
                f"Registry release is incomplete: {project.name}=={project.version}"
            )
        else:
            pass


def check_dependency(manifest: ReleaseManifest, json_url: str) -> None:
    if manifest.package == "vlah":
        check_registry(
            Project(name="vlah-testbed", version=manifest.versions["vlah-testbed"]),
            manifest.dependency_files,
            json_url,
            require_complete=True,
        )
    else:
        pass


def publish_pypi(
    directory: Path,
    manifest: ReleaseManifest,
    index_url: str,
    publish_url: str,
    json_url: str,
    dry_run: bool,
) -> None:
    if dry_run:
        publishing_options = ["--dry-run", "--trusted-publishing", "never"]
    elif manifest.package is None or not manifest.publish:
        raise ValueError("A build-only bundle cannot publish")
    else:
        check_dependency(manifest, json_url)
        check_registry(
            Project(name=manifest.package, version=manifest.versions[manifest.package]),
            manifest.files,
            json_url,
            require_complete=False,
        )
        publishing_options = ["--trusted-publishing", "always"]
    subprocess.run(
        [
            "uv",
            "publish",
            "--no-config",
            "--check-url",
            registry_url(index_url),
            "--publish-url",
            registry_url(publish_url),
            *publishing_options,
            *[str(directory / filename) for filename in sorted(manifest.files)],
        ],
        check=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if dry_run:
        return
    elif manifest.package is None:
        raise ValueError("Publication requires a selected package")
    else:
        check_registry(
            Project(name=manifest.package, version=manifest.versions[manifest.package]),
            manifest.files,
            json_url,
            require_complete=True,
        )


class GitHubAsset(BaseModel):
    name: str
    state: str
    size: int


class GitHubRelease(BaseModel):
    tag_name: str
    target_commitish: str
    draft: bool
    prerelease: bool
    body: str | None
    assets: list[GitHubAsset]


def find_github_release(
    manifest: ReleaseManifest, repository_root: Path
) -> GitHubRelease | None:
    pages = TypeAdapter(list[list[GitHubRelease]]).validate_json(
        command_output(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{manifest.repository}/releases?per_page=100",
            ],
            repository_root,
        )
    )
    releases = [
        release
        for page in pages
        for release in page
        if release.tag_name == manifest.tag
    ]
    if len(releases) > 1:
        raise ValueError("Multiple GitHub releases identify the same tag")
    else:
        return releases[0] if releases else None


def check_github_assets(
    release: GitHubRelease,
    manifest: ReleaseManifest,
    directory: Path,
    repository_root: Path,
) -> set[str]:
    manifest_sha256 = hashlib.sha256(
        (directory / "release.json").read_bytes()
    ).hexdigest()
    expected = dict(manifest.files, **{"release.json": manifest_sha256})
    if (
        release.tag_name != manifest.tag
        or release.target_commitish != manifest.commit_sha
        or release.prerelease != manifest.is_prerelease
        or release.body is None
        or f"Artifact manifest SHA-256: `{manifest_sha256}`" not in release.body
    ):
        raise ValueError(
            "Existing GitHub release does not identify this validated bundle"
        )
    else:
        present: set[str] = set()
        with TemporaryDirectory(prefix="vlah-release-download-") as temporary:
            for asset in release.assets:
                if (
                    asset.name not in expected
                    or asset.name in present
                    or asset.state != "uploaded"
                    or asset.size != (directory / asset.name).stat().st_size
                ):
                    raise ValueError(
                        f"Unexpected or incomplete GitHub asset: {asset.name}"
                    )
                else:
                    command_output(
                        [
                            "gh",
                            "release",
                            "download",
                            release.tag_name,
                            "--repo",
                            manifest.repository,
                            "--pattern",
                            asset.name,
                            "--dir",
                            temporary,
                        ],
                        repository_root,
                    )
                    digest = hashlib.sha256(
                        (Path(temporary) / asset.name).read_bytes()
                    ).hexdigest()
                    if digest != expected[asset.name]:
                        raise ValueError(
                            f"GitHub asset differs from validated bytes: {asset.name}"
                        )
                    else:
                        present.add(asset.name)
        return set(expected) - present


def publish_github(
    directory: Path, manifest: ReleaseManifest, repository_root: Path
) -> None:
    if manifest.tag is None or manifest.package is None or not manifest.publish:
        raise ValueError("A build-only bundle cannot create a GitHub release")
    else:
        release = find_github_release(manifest, repository_root)
        if release is None:
            manifest_sha256 = hashlib.sha256(
                (directory / "release.json").read_bytes()
            ).hexdigest()
            version = manifest.versions[manifest.package]
            stage = "Prerelease" if manifest.is_prerelease else "Release"
            notes = (
                f"{stage} of **{manifest.package} {version}**. "
                "Only this package is published by this tag.\n\n"
                f"Install: `python -m pip install '{manifest.package}=={version}'`\n\n"
                f"Source commit: `{manifest.commit_sha}`\n\n"
                f"Artifact manifest SHA-256: `{manifest_sha256}`\n\n"
                "The wheel and source archive are the same validated bytes uploaded to PyPI."
            )
            command_output(
                [
                    "gh",
                    "release",
                    "create",
                    manifest.tag,
                    "--repo",
                    manifest.repository,
                    "--verify-tag",
                    "--target",
                    manifest.commit_sha,
                    "--draft",
                    f"--prerelease={str(manifest.is_prerelease).lower()}",
                    "--latest=false",
                    "--title",
                    f"{manifest.package} {version}",
                    "--notes",
                    notes,
                ],
                repository_root,
            )
            release = find_github_release(manifest, repository_root)
            if release is None:
                raise RuntimeError("Created GitHub release is not readable")
            else:
                pass
        else:
            pass
        missing = check_github_assets(release, manifest, directory, repository_root)
        if not release.draft and missing:
            raise ValueError(
                "Published GitHub release is missing assets; refusing to alter it"
            )
        else:
            for filename in sorted(missing):
                command_output(
                    [
                        "gh",
                        "release",
                        "upload",
                        manifest.tag,
                        str(directory / filename),
                        "--repo",
                        manifest.repository,
                    ],
                    repository_root,
                )
            release = find_github_release(manifest, repository_root)
            if release is None or check_github_assets(
                release, manifest, directory, repository_root
            ):
                raise RuntimeError("GitHub asset upload did not complete")
            elif release.draft:
                command_output(
                    [
                        "gh",
                        "release",
                        "edit",
                        manifest.tag,
                        "--repo",
                        manifest.repository,
                        "--verify-tag",
                        "--draft=false",
                        "--latest=false",
                    ],
                    repository_root,
                )
            else:
                pass
            release = find_github_release(manifest, repository_root)
            if (
                release is None
                or release.draft
                or check_github_assets(release, manifest, directory, repository_root)
            ):
                raise RuntimeError("GitHub release publication did not complete")
            else:
                pass
