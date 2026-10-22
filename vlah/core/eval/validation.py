"""What a request must satisfy before anything is written or spent (v2.2 D37, D63, D69).

The baseline a candidate is measured against is the one the request names, or else
the one its manifest says it was built on. That is also the target the worker's
own fast check hashed, so both sides agree on the candidate's hash (D69). A
baseline the blackboard does not know is refused before a judge run is spent.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from vlah.core.blackboard.api import Blackboard
from vlah.core.eval.jobs import load_manifest
from vlah.core.types.candidates import CandidateManifest, payload_files
from vlah.ports.signal_source import SignalSource

MAX_CANDIDATE_BYTES = 5 * 1024 * 1024
BASELINE_PATTERN = re.compile(r"baseline@(?P<number>\d+)")


@dataclass(frozen=True, kw_only=True)
class CheckedRequest:
    manifest: CandidateManifest
    baseline: str


def symbolic_link_in(directory: Path) -> Path | None:
    """The first symbolic link under directory; hashing or copying it would follow it."""
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            return path
        else:
            continue
    return None


def baseline_exists(blackboard: Blackboard, baseline: str) -> bool:
    match = BASELINE_PATTERN.fullmatch(baseline)
    if match is None:
        return False
    elif match.group("number") == "0":
        return True
    else:
        record = f"baseline/{match.group('number')}.json"
        return record in blackboard.list_paths(record, None)


def check_candidate(
    blackboard: Blackboard,
    candidate_directory: Path,
    requested_baseline: str | None,
) -> CheckedRequest | str:
    """The manifest and the baseline to measure against, or why the request is refused."""
    if not (candidate_directory / "manifest.yaml").is_file():
        return "the candidate directory has no manifest.yaml"
    else:
        link = symbolic_link_in(candidate_directory)
        if link is not None:
            return (
                f"the candidate contains a symbolic link "
                f"({link.relative_to(candidate_directory)})"
            )
        else:
            try:
                manifest = load_manifest(candidate_directory)
            except ValueError as error:
                return f"manifest.yaml is not valid: {error}"
            baseline = requested_baseline or manifest.target
            if not baseline_exists(blackboard, baseline):
                return f"unknown baseline {baseline}"
            else:
                return CheckedRequest(manifest=manifest, baseline=baseline)


def request_error(
    blackboard: Blackboard,
    ladder: tuple[SignalSource, ...],
    candidate_directory: Path,
    predictions: dict[str, float],
    rationale: str,
    parents: tuple[str, ...],
) -> str | None:
    """A worker request's own rules: predictions, a rationale, size, known parents (D37)."""
    missing_levels = [
        source.declaration.id
        for source in ladder
        if source.declaration.id not in predictions
    ]
    payload_bytes = sum(
        file_path.stat().st_size
        for relative_path, file_path in payload_files(candidate_directory)
    )
    unknown_parents = [
        parent
        for parent in parents
        if not blackboard.list_paths(f"candidates/{parent}/", None)
    ]
    if missing_levels:
        return (
            f"predictions are required for every level: missing {missing_levels} (D37)"
        )
    elif not rationale.strip():
        return "a rationale is required (D37)"
    elif payload_bytes > MAX_CANDIDATE_BYTES:
        return f"the candidate is {payload_bytes} bytes; the limit is {MAX_CANDIDATE_BYTES}"
    elif unknown_parents:
        return f"unknown parent candidates {unknown_parents}"
    else:
        return None
