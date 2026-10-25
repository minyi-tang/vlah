"""What each worker's clone of the blackboard holds, by arm (D58, D67).

A rule cannot stop a worker with a shell from reading its clone, so isolation is
physical: in S1 and B1 each worker clones its own read-only mirror, which holds only
what that worker may see. S2 and B0 workers clone the run's blackboard.
- S1: spec.md, approaches/, every candidate and the eval service's result events,
  and the worker's own prefix. The eval service's result claims stay out: a result
  claim's body is the submitter's rationale, which S1 shares no more than B1 does.
  The mirror holds exactly what the tool scope lets an S1 worker read.
- B1: spec.md, approaches/, the worker's own prefix, and the candidates the worker
  submitted (with their results).

One eval service and one Integrator serve every worker of a run either way, so the
run keeps one judge quota (C3) and B1's verified candidates still merge in the
background (D58).
"""

from collections.abc import Mapping
from typing import Protocol

import yaml

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard, PathFilter
from vlah.core.types.ids import ActorId

OWN_PREFIXES = ("claims", "events", "traces", "checkpoints")
EVAL_SERVICE = "eval_service"


def is_problem_file(path: str) -> bool:
    return path == "spec.md" or path.startswith("approaches/")


def is_own_path(agent: ActorId, path: str) -> bool:
    segments = path.split("/")
    return len(segments) >= 3 and segments[0] in OWN_PREFIXES and segments[1] == agent


def is_result_event(path: str) -> bool:
    return path.startswith(f"events/{EVAL_SERVICE}/")


class ScoresView:
    """S1: candidates and scores, but no other worker's claims or rationales."""

    def __init__(self, agent: ActorId) -> None:
        self.agent = agent

    def __call__(self, path: str) -> bool:
        return (
            is_problem_file(path)
            or is_own_path(self.agent, path)
            or path.startswith("candidates/")
            or is_result_event(path)
        )


class OwnWorkView:
    """B1: only the worker's own claims and the candidates it submitted.

    A candidate's author is read from its manifest once and cached: manifests are
    append-only, so the answer never changes.
    """

    def __init__(self, agent: ActorId, blackboard: Blackboard) -> None:
        self.agent = agent
        self.blackboard = blackboard
        self.is_mine_by_hash: dict[str, bool] = {}

    def submitted(self, path: str) -> bool:
        segments = path.split("/")
        if len(segments) < 3 or segments[0] != "candidates":
            return False
        elif segments[1] in self.is_mine_by_hash:
            return self.is_mine_by_hash[segments[1]]
        else:
            manifest_path = f"candidates/{segments[1]}/manifest.yaml"
            if manifest_path not in self.blackboard.list_paths(manifest_path, None):
                return False
            else:
                manifest: object = yaml.safe_load(
                    self.blackboard.read(manifest_path, None)
                )
                is_mine = (
                    isinstance(manifest, dict) and manifest.get("author") == self.agent
                )
                self.is_mine_by_hash[segments[1]] = is_mine
                return is_mine

    def __call__(self, path: str) -> bool:
        return (
            is_problem_file(path)
            or is_own_path(self.agent, path)
            or self.submitted(path)
        )


def worker_view(
    blackboard: Blackboard, arm: ArmConfig, run_id: str, agent: ActorId
) -> Blackboard:
    """The blackboard this worker clones: a mirror of its own in S1 and B1."""
    keep: PathFilter | None
    if arm.repo_read_scope == "spec_approaches_own_candidates":
        keep = ScoresView(agent)
    elif arm.repo_read_scope == "spec_approaches_own":
        keep = OwnWorkView(agent, blackboard)
    else:
        keep = None
    if keep is None:
        return blackboard
    else:
        return blackboard.mirror(f"{run_id}-{agent}", keep)


class ViewPublisher(Protocol):
    """Serves each worker's view over the network (#129): the stack's blackboard."""

    def publish(self, run_id: str, agent: ActorId, view: Blackboard) -> str:
        """Make the view reachable; the URL the worker's sandbox clones."""
        ...

    def settings(self, agent: ActorId) -> Mapping[str, str]:
        """Git settings that open that worker's view and no other's."""
        ...
