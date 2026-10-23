"""Opening a run's blackboard and laying out a problem on it (v2.2 section 5, D63, D67).

Each run gets its own git repository, and each B1 worker its own, under
<root>/<run_id>/. Git does not track empty directories, so the section 5 layout
exists as soon as its first file is written.
"""

import secrets
from dataclasses import dataclass
from pathlib import Path

from vlah.adapters.store.git.store import GitVersionedStore
from vlah.core.blackboard.api import Blackboard, PublicProblemFiles
from vlah.core.blackboard.blackboard import StoreBlackboard
from vlah.core.types.clock import SystemClock
from vlah.core.types.ids import ActorId
from vlah.ports.store import ObjectStore, VersionedStore

PROBLEM_AUTHOR = ActorId("human")
NEVER_PUBLIC_NAMES = ("herd.yaml",)
NEVER_PUBLIC_PREFIXES = ("private/",)


@dataclass(frozen=True, kw_only=True)
class GitMirrorStoreFactory:
    run_directory: Path
    blackboard_name: str

    def __call__(self, mirror_name: str) -> tuple[VersionedStore, str]:
        push_token = secrets.token_hex(16)
        store = GitVersionedStore(
            repository_path=self.run_directory
            / f"{self.blackboard_name}-{mirror_name}.git",
            push_token=push_token,
        )
        return store, push_token


def open_blackboard(
    root: Path, run_id: str, name: str, *, objects: ObjectStore | None = None
) -> Blackboard:
    """A git-backed blackboard at <root>/<run_id>/<name>.git.

    The push token is generated here and kept in memory: the herd writes through git
    plumbing, so no process outside this one ever needs it. Without an object store,
    object:// and private:// evidence never resolves.
    """
    run_directory = root / run_id
    push_token = secrets.token_hex(16)
    return StoreBlackboard(
        store=GitVersionedStore(
            repository_path=run_directory / f"{name}.git", push_token=push_token
        ),
        push_token=push_token,
        objects=objects,
        mirror_store_factory=GitMirrorStoreFactory(
            run_directory=run_directory, blackboard_name=name
        ),
        clock=SystemClock(),
    )


def is_never_public(path: str) -> bool:
    return path in NEVER_PUBLIC_NAMES or path.startswith(NEVER_PUBLIC_PREFIXES)


def init_layout(blackboard: Blackboard, problem_files: PublicProblemFiles) -> None:
    """Write the problem's public files to an empty blackboard, as the human author.

    Refuses herd.yaml and anything under private/ even if a caller passes them: workers
    clone the blackboard, and git cannot hide a path from a clone (D63).
    """
    leaked_paths = sorted(path for path in problem_files.files if is_never_public(path))
    if leaked_paths:
        raise ValueError(f"never copy these to the blackboard: {leaked_paths}")
    elif not problem_files.files:
        raise ValueError(f"the problem at {problem_files.root} has no public files")
    else:
        receipt = blackboard.write(
            PROBLEM_AUTHOR, problem_files.files, "lay out the problem"
        )
        if receipt.status == "rejected":
            raise ValueError(f"the problem files were rejected: {receipt.violations}")
        else:
            pass
