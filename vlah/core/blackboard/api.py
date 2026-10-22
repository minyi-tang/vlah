"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

The blackboard: one git repository, the only source of truth (v2.2 D30, D31, D56).
Implemented by W1-01 (#11).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from vlah.core.types.claims import Claim
from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId


@dataclass(frozen=True, kw_only=True)
class Violation:
    """Why a write was rejected: which path, which rule, in words."""

    path: str
    rule: Literal[
        "path_not_allowed",
        "file_exists",
        "invalid_claim",
        "invalid_event",
        "invalid_manifest",
        "missing_evidence",
        "dangling_evidence",
    ]
    message: str


@dataclass(frozen=True, kw_only=True)
class WriteReceipt:
    status: Literal["merged", "queued", "rejected"]
    commit_id: str | None
    sequence_numbers: tuple[int, ...]
    violations: tuple[Violation, ...]


@dataclass(frozen=True, kw_only=True)
class MergedCommit:
    """A commit on main, parsed: its events carry the sequence numbers they were given."""

    commit_id: str
    actor: ActorId
    paths: tuple[str, ...]
    events: tuple[Event, ...]
    claims: tuple[Claim, ...]


class CommitListener(Protocol):
    def __call__(self, merged_commit: MergedCommit) -> None: ...


class PathFilter(Protocol):
    def __call__(self, path: str) -> bool: ...


class Blackboard(Protocol):
    def write(
        self, actor: ActorId, files: Mapping[str, bytes], message: str
    ) -> WriteReceipt:
        """Check, commit and merge new files written by actor.

        Called by the tool server on a worker's behalf and by system roles. A rejected
        write commits nothing.
        """
        ...

    def check(
        self, actor: ActorId, files: Mapping[str, bytes]
    ) -> tuple[Violation, ...]:
        """The synchronous checks of write, without writing (D31, D33)."""
        ...

    def read(self, path: str, at_sequence: int | None) -> bytes: ...

    def list_paths(self, prefix: str, at_sequence: int | None) -> tuple[str, ...]: ...

    def head_sequence(self) -> int: ...

    def subscribe(self, listener: CommitListener) -> int:
        """Call listener after every merge, in sequence order; returns a subscription id."""
        ...

    def unsubscribe(self, subscription_id: int) -> None: ...

    def mirror(self, name: str, keep: PathFilter) -> "Blackboard":
        """A read-only copy that holds only the kept paths and follows every merge (D67)."""
        ...

    def clone_url(self) -> str:
        """Read-only clone URL for workers; they never get push credentials (D68)."""
        ...


@dataclass(frozen=True, kw_only=True)
class PublicProblemFiles:
    """The worker-visible part of a problem folder, never herd.yaml or private/."""

    root: Path
    files: dict[str, bytes]


class OpenBlackboard(Protocol):
    def __call__(self, root: Path, run_id: str, name: str) -> Blackboard:
        """One blackboard per run, and one per B1 worker (D67)."""
        ...


class InitLayout(Protocol):
    def __call__(
        self, blackboard: Blackboard, problem_files: PublicProblemFiles
    ) -> None:
        """Create the section 5 layout and copy the public problem files."""
        ...
