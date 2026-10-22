"""Who may read and create what through the tools, by arm (D31, D58, D63).

The blackboard checks writes again; these gates come first so a worker gets a clear
reason, and the reads are the tool server's alone.
"""

import re
from pathlib import Path

import yaml

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.ids import ActorId

PRIVATE_POINTER_PREFIX = "private://"
OWN_PREFIXES = ("claims", "events", "traces", "checkpoints")
MAP_SCRIPT_PATTERN = re.compile(r"map/measurements/[^/]+/[^/]+")
CANDIDATE_POINTER_PATTERN = re.compile(r"candidates/(?P<hash>[0-9a-f]+)/")


def is_own_path(agent: ActorId, path: str) -> bool:
    segments = path.split("/")
    return len(segments) >= 3 and segments[0] in OWN_PREFIXES and segments[1] == agent


def submitted_by(blackboard: Blackboard, agent: ActorId, path: str) -> bool:
    """Whether path is inside a candidate this agent submitted (B1 sees its own results)."""
    match = CANDIDATE_POINTER_PATTERN.match(path)
    if match is None:
        return False
    else:
        manifest_path = f"candidates/{match.group('hash')}/manifest.yaml"
        if manifest_path not in blackboard.list_paths(manifest_path, None):
            return False
        else:
            manifest_data: object = yaml.safe_load(blackboard.read(manifest_path, None))
            return CandidateManifest.model_validate(manifest_data).author == agent


def may_read(blackboard: Blackboard, arm: ArmConfig, agent: ActorId, path: str) -> bool:
    """repo_read scope by arm (D58); private data is never readable by a worker (D63)."""
    scope = arm.repo_read_scope
    is_shared_problem_file = path == "spec.md" or path.startswith("approaches/")
    if path.startswith(PRIVATE_POINTER_PREFIX):
        return False
    elif scope == "all":
        return True
    elif is_shared_problem_file or is_own_path(agent, path):
        return True
    elif scope == "spec_approaches_own_candidates":
        return path.startswith("candidates/") or path.startswith("events/eval_service/")
    else:
        return submitted_by(blackboard, agent, path)


def write_gate(arm: ArmConfig, agent: ActorId, path: str) -> str | None:
    """The tool server's own gate, before the blackboard's (D31)."""
    if is_own_path(agent, path):
        return None
    elif MAP_SCRIPT_PATTERN.fullmatch(path) and arm.map == "full":
        return None
    else:
        return (
            f"{path}: workers create files only under their own prefix "
            f"(claims/{agent}/ ...)"
        )


def symbolic_link_in(directory: Path) -> Path | None:
    """The first symbolic link under directory: following one could copy any host file."""
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            return path
        else:
            continue
    return None


def resolve_candidate_directory(sandbox_root: Path, candidate_dir: str) -> Path | str:
    """The candidate directory inside the sandbox, or why it is refused (D63)."""
    root = sandbox_root.resolve()
    directory = (root / candidate_dir).resolve()
    if not (directory.is_relative_to(root) and directory.is_dir()):
        return f"{candidate_dir} is not a directory in your sandbox"
    else:
        link = symbolic_link_in(directory)
        if link is not None:
            return (
                f"{candidate_dir} contains a symbolic link "
                f"({link.relative_to(directory)}); copy the file in instead"
            )
        else:
            return directory
