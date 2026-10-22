"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

The eval client: the only component that spends judge runs (v2.2 section 9, D26,
D37, D54, D57, D73, D74). Implemented by W1-04 (#14).
"""

from pathlib import Path
from typing import Literal, Protocol

from vlah.core.types.base import FrozenModel
from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId

EvalStatus = Literal[
    "queued", "needs_confirmation", "dedup_hit", "gate_failed", "invalid_request"
]


class EvalRequest(FrozenModel):
    """What a worker sends with eval_request; predictions are keyed by source id (D37)."""

    candidate_directory: Path
    parents: tuple[str, ...] = ()
    predictions: dict[str, float]
    rationale: str
    confirm_token: str | None = None
    baseline: str | None = None


class SimilarItem(FrozenModel):
    item_id: str
    kind: Literal["candidate", "dead_end"]
    similarity: float
    summary: str


class EvalResponse(FrozenModel):
    request_id: str
    claim_id: str | None
    baseline: str
    status: EvalStatus
    similar: tuple[SimilarItem, ...] = ()
    confirm_token: str | None = None
    degraded_sources: tuple[str, ...] = ()
    error: str | None = None


class RemeasureRequest(FrozenModel):
    """One paired remeasure at the top level, for the Integrator (D74, TB section 11.8.1).

    candidate_directory holds the candidate already rebased onto baseline; it is a new
    candidate, written by the Integrator, with parent_candidate as its lineage.
    """

    candidate_directory: Path
    baseline: str
    parent_candidate: str
    approach: str


class EvalService(Protocol):
    def request(self, request: EvalRequest, caller: ActorId) -> EvalResponse:
        """Validate, search for similar work, run gates, dedup, and queue (D57)."""
        ...

    def remeasure(self, request: RemeasureRequest) -> str:
        """Integrator only: one top-level paired run that skips confirmation and dedup,
        charged to the Integrator's quota (D74). The result arrives as a remeasure_ready
        event; the return value is its request_id."""
        ...

    def tick(self) -> tuple[Event, ...]:
        """Poll the judges, advance the ladder, and write the resulting events."""
        ...
