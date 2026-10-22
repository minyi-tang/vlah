"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

The indexer: replays events into current state and answers "what changed that
matters to me?" and "has anyone tried this?" (v2.2 D9, D13, D18, D34).
Implemented by W1-02 (#12).
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import MergedCommit
from vlah.core.types.ids import ActorId

HitKind = Literal["claim", "candidate", "digest", "trace", "charter"]


@dataclass(frozen=True, kw_only=True)
class StatusChange:
    sequence: int
    actor: ActorId
    from_status: str
    to_status: str


@dataclass(frozen=True, kw_only=True)
class ClaimState:
    claim_id: str
    status: str
    history: tuple[StatusChange, ...]
    is_cascaded_refuted: bool


@dataclass(frozen=True, kw_only=True)
class ApproachState:
    approach_id: str
    status: str
    history: tuple[StatusChange, ...]


@dataclass(frozen=True, kw_only=True)
class Hit:
    item_id: str
    kind: HitKind
    similarity: float
    summary: str


@dataclass(frozen=True, kw_only=True)
class WorkingSet:
    """The lease's approach and target claims, plus ids the agent recently searched (D18)."""

    approach: str
    target_claims: tuple[str, ...]
    recent_ids: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class DiffItem:
    item_id: str
    kind: HitKind
    approach: str
    status: str
    summary: str


@dataclass(frozen=True, kw_only=True)
class Diff:
    """Ranked changes since a cursor, truncated to a token budget, ending in "N more"."""

    items: tuple[DiffItem, ...]
    cursor: int
    omitted_count: int
    rendered: str


@dataclass(frozen=True, kw_only=True)
class IndexerMetrics:
    """Two of the four standing metrics (section 11)."""

    dead_end_rewalk_count: int
    insight_reuse_counts: dict[str, int]


class Indexer(Protocol):
    def apply(self, merged_commit: MergedCommit) -> None:
        """Fold one merge into state; applying the same sequence numbers twice is a no-op."""
        ...

    def rebuild(self) -> None:
        """Replay from sequence 0; the result must equal the incremental state."""
        ...

    def claim_state(self, claim_id: str) -> ClaimState: ...

    def approach_state(self, approach_id: str) -> ApproachState: ...

    def search(
        self, query: str, limit: int, viewer: ActorId, arm: ArmConfig
    ) -> tuple[Hit, ...]:
        """Semantic search, scoped by arm.search; B1 always gets nothing."""
        ...

    def similar_candidates(
        self, payload_text: str, limit: int, viewer: ActorId
    ) -> tuple[Hit, ...]:
        """Compares payloads only, never targets (D69)."""
        ...

    def similar_dead_ends(
        self, payload_text: str, limit: int, viewer: ActorId
    ) -> tuple[Hit, ...]:
        """Confirmed dead ends only (D26)."""
        ...

    def diff_since(
        self,
        cursor: int,
        working_set: WorkingSet,
        viewer: ActorId,
        max_tokens: int,
        arm: ArmConfig,
    ) -> Diff: ...

    def reuse_count(self, claim_id: str) -> int: ...

    def metrics(self) -> IndexerMetrics: ...
