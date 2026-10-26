"""A release retry may add missing draft assets but may never replace existing bytes."""

import json
from pathlib import Path

import pytest

from scripts.release_plan import ReleaseManifest
from scripts.release_publish import GitHubAsset, GitHubRelease, publish_github

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class GitHubCommands:
    def __init__(self, manifest: ReleaseManifest) -> None:
        self.manifest = manifest
        self.release: GitHubRelease | None = None
        self.files: dict[str, bytes] = {}
        self.mutations: list[list[str]] = []
        self.interrupt_upload = False

    def invoke(self, command: list[str], repository_root: Path) -> str:
        assert repository_root == REPOSITORY_ROOT
        if command[:2] == ["gh", "api"]:
            return json.dumps([[self.release.model_dump()] if self.release else []])
        elif command[2] == "create":
            assert self.release is None
            assert "--verify-tag" in command and "--draft" in command
            assert command[3] == self.manifest.tag
            self.mutations.append(command)
            self.release = GitHubRelease(
                tag_name=command[3],
                target_commitish=command[command.index("--target") + 1],
                draft=True,
                prerelease="--prerelease=true" in command,
                body=command[command.index("--notes") + 1],
                assets=[],
            )
        elif command[2] == "download":
            filename = command[command.index("--pattern") + 1]
            directory = Path(command[command.index("--dir") + 1])
            (directory / filename).write_bytes(self.files[filename])
        elif command[2] == "upload":
            assert "--clobber" not in command
            path = Path(command[4])
            assert path.name not in self.files
            assert self.release is not None and self.release.draft
            if self.interrupt_upload and self.files:
                raise RuntimeError("Interrupted after a partial upload")
            else:
                self.mutations.append(command)
                self.files[path.name] = path.read_bytes()
                self.release.assets.append(
                    GitHubAsset(
                        name=path.name, state="uploaded", size=path.stat().st_size
                    )
                )
        elif command[2] == "edit":
            assert self.release is not None
            assert "--draft=false" in command and "--latest=false" in command
            self.mutations.append(command)
            self.release.draft = False
        else:
            raise AssertionError(f"Unexpected GitHub command: {command}")
        return ""


def test_draft_recovery_uploads_only_missing_assets_and_published_retry_is_read_only(
    release_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = ReleaseManifest.model_validate_json(
        (release_bundle / "release.json").read_bytes()
    )
    github = GitHubCommands(manifest)
    github.interrupt_upload = True
    monkeypatch.setattr("scripts.release_publish.command_output", github.invoke)
    with pytest.raises(RuntimeError, match="partial upload"):
        publish_github(release_bundle, manifest, REPOSITORY_ROOT)
    assert github.release is not None and github.release.draft
    assert len(github.files) == 1
    github.interrupt_upload = False
    publish_github(release_bundle, manifest, REPOSITORY_ROOT)
    assert not github.release.draft
    expected = {
        name: (release_bundle / name).read_bytes()
        for name in [*manifest.files, "release.json"]
    }
    assert github.files == expected
    mutations = list(github.mutations)
    publish_github(release_bundle, manifest, REPOSITORY_ROOT)
    assert github.mutations == mutations
    assert github.files == expected


@pytest.mark.parametrize(
    "conflict",
    [
        "source",
        "manifest",
        "prerelease",
        "content",
        "extra",
        "incomplete-published",
        "placeholder",
    ],
)
def test_conflicting_existing_releases_fail_without_mutating_assets(
    release_bundle: Path, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    manifest = ReleaseManifest.model_validate_json(
        (release_bundle / "release.json").read_bytes()
    )
    github = GitHubCommands(manifest)
    monkeypatch.setattr("scripts.release_publish.command_output", github.invoke)
    publish_github(release_bundle, manifest, REPOSITORY_ROOT)
    assert github.release is not None
    filename = sorted(manifest.files)[0]
    if conflict == "source":
        github.release.target_commitish = "b" * 40
    elif conflict == "manifest":
        github.release.body = "Unrelated existing release"
    elif conflict == "prerelease":
        github.release.prerelease = True
    elif conflict == "content":
        github.files[filename] = b"x" * len(github.files[filename])
    elif conflict == "extra":
        github.files["unexpected.txt"] = b"extra"
        github.release.assets.append(
            GitHubAsset(name="unexpected.txt", state="uploaded", size=5)
        )
    elif conflict == "incomplete-published":
        github.release.assets = [
            asset for asset in github.release.assets if asset.name != filename
        ]
        github.files.pop(filename)
    else:
        github.release.draft = True
        github.release.assets[0].state = "starter"
    mutations = list(github.mutations)
    expected_files = dict(github.files)
    with pytest.raises(ValueError):
        publish_github(release_bundle, manifest, REPOSITORY_ROOT)
    assert github.mutations == mutations
    assert github.files == expected_files
