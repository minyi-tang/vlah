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


class EvalService(Protocol):
    def request(self, request: EvalRequest, caller: ActorId) -> EvalResponse:
        """Validate, search for similar work, run gates, dedup, and queue (D57)."""
        ...

    def remeasure(self, candidate_hash: str, baseline: str) -> str:
        """Integrator only: a paired remeasure that skips confirmation and dedup (D74)."""
        ...

    def tick(self) -> tuple[Event, ...]:
        """Poll the judges, advance the ladder, and write the resulting events."""
        ...
