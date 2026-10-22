"""Candidate manifests and the candidate hash (v2.2 D69, D73; Herd Testbed section 11.2).

The hash must match the testbed's byte for byte, because dedup, the quota-discipline
metric and Integrator remeasures all key on it. The layout, sorted by POSIX relative path:
for each file, the UTF-8 path, one NUL byte, then the raw file bytes; after the last
file, the UTF-8 target baseline id. The result is lowercase SHA-256 hex.
"""

import hashlib
from pathlib import Path
from typing import Literal

from vlah.core.types.base import FrozenModel
from vlah.core.types.ids import ActorId

MANIFEST_FILE_NAME = "manifest.yaml"
EXCLUDED_DIRECTORY_NAME = "__pycache__"
EXCLUDED_SUFFIX = ".pyc"


class CandidateManifest(FrozenModel):
    """candidates/<hash>/manifest.yaml; target is a baseline id, never a repo sha."""

    kind: Literal["patch", "config", "artifact"]
    target: str
    parents: tuple[str, ...] = ()
    approach: str
    author: ActorId
    created_at: float
    direction: str | None = None
    predicted_score: float | None = None
    note: str | None = None


def payload_files(payload_directory: Path) -> list[tuple[str, Path]]:
    """Files that make up a candidate's payload, as (POSIX relative path, path), sorted."""
    selected: list[tuple[str, Path]] = []
    for file_path in payload_directory.rglob("*"):
        relative_path = file_path.relative_to(payload_directory)
        if not file_path.is_file():
            continue
        elif EXCLUDED_DIRECTORY_NAME in relative_path.parts:
            continue
        elif file_path.suffix == EXCLUDED_SUFFIX:
            continue
        elif relative_path.as_posix() == MANIFEST_FILE_NAME:
            continue
        else:
            selected.append((relative_path.as_posix(), file_path))
    return sorted(selected)


def candidate_hash(payload_directory: Path, target: str) -> str:
    """Hash of baseline id plus payload: the same diff on another baseline is a new candidate."""
    digest = hashlib.sha256()
    for relative_path, file_path in payload_files(payload_directory):
        digest.update(relative_path.encode("utf-8") + b"\0" + file_path.read_bytes())
    digest.update(target.encode("utf-8"))
    return digest.hexdigest()


def payload_text(payload_directory: Path) -> str:
    """Payload as text for similarity search; it ignores the target on purpose (D69)."""
    sections = [
        f"{relative_path}\n{file_path.read_text(encoding='utf-8', errors='replace')}"
        for relative_path, file_path in payload_files(payload_directory)
    ]
    return "\n".join(sections)
