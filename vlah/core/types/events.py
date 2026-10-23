"""Events: the only way state changes on the blackboard (v2.2 D56, D74, D76).

Writers leave seq empty; the merge queue assigns it. The payload class is chosen
by the event type, never guessed from the payload's fields.
"""

from typing import Literal, Self

from pydantic import Field, ValidationInfo, field_validator, model_validator

from vlah.core.types.base import FrozenModel
from vlah.core.types.ids import ActorId, Role
from vlah.core.types.sources import Paired

EventType = Literal[
    "claim_status",
    "approach_status",
    "result_ready",
    "surge_start",
    "surge_end",
    "plateau",
    "escalation",
    "lease_stale",
    "allocation",
    "reinstated",
    "baseline_advanced",
    "baseline_reverted",
    "integration_failed",
    "interaction_found",
    "map_updated",
    "audit_checked",
    "dedup_hit",
    "stopped",
    "remeasure_ready",
]


class ResultReady(FrozenModel):
    """approach is the candidate's approach, so the follow-up task can name it (D28).

    constraint_ok False means the result was refuted by a constraint or a
    post-result gate (D54): paired is then a measurement, never a gain. score and
    paired are None when the run was void, for example a candidate that crashed or
    timed out on a seed; constraint_ok is then False too.
    """

    candidate: str
    claim_id: str
    approach: str
    submitter: ActorId
    submitter_checkpoint: str | None
    baseline: str
    paired: Paired | None
    per_item: str
    score: float | None
    level: str
    constraint_ok: bool | None = None


class BaselineAdvanced(FrozenModel):
    baseline: int
    parent_baseline: int
    candidate: str
    files_changed: tuple[str, ...]
    paired: Paired
    guard_pending: bool


class BaselineReverted(FrozenModel):
    from_baseline: int
    new_baseline: int
    removed: tuple[str, ...]
    reason: str


class IntegrationFailed(FrozenModel):
    candidate: str
    baseline: int
    paired: Paired | None
    reason: str


class InteractionFound(FrozenModel):
    candidate: str
    baseline: int
    earlier: Paired
    remeasured: Paired
    overlaps_with: tuple[str, ...]


class AuditChecked(FrozenModel):
    segment: tuple[int, int]
    daily: Paired
    audit: Paired
    threshold: float
    flagged: bool


class MapUpdated(FrozenModel):
    baseline: int
    measurements: tuple[str, ...]
    output: str


class DedupHit(FrozenModel):
    candidate: str
    original_claim: str


class RemeasureReady(FrozenModel):
    """The Integrator's paired remeasure of a rebased candidate on baseline@k (D74).

    It goes to the Integrator only; it never creates a follow-up task for a worker.
    As in ResultReady, score and paired are None when the run was void, and
    constraint_ok False means the remeasure was refuted. per_seed is the run's own
    per-seed scores: result.json is written once per hash, so a second measurement
    of the same content and target (a reverted baseline measured on the daily set
    and then on the audit set) has no record of its own there. dataset says which
    set the judge ran it on (D72).
    """

    request_id: str
    candidate: str
    parent_candidate: str
    claim_id: str
    baseline: str
    paired: Paired | None
    score: float | None
    level: str
    constraint_ok: bool | None = None
    per_seed: tuple[float, ...] = ()
    dataset: Literal["dev", "audit"] = "dev"


class Stopped(FrozenModel):
    reason: Literal["budget", "target", "all_frozen", "plateau", "human", "time"]


EventPayload = (
    ResultReady
    | BaselineAdvanced
    | BaselineReverted
    | IntegrationFailed
    | InteractionFound
    | AuditChecked
    | MapUpdated
    | DedupHit
    | Stopped
    | RemeasureReady
)

PAYLOAD_MODELS: dict[str, type[EventPayload]] = {
    "result_ready": ResultReady,
    "baseline_advanced": BaselineAdvanced,
    "baseline_reverted": BaselineReverted,
    "integration_failed": IntegrationFailed,
    "interaction_found": InteractionFound,
    "audit_checked": AuditChecked,
    "map_updated": MapUpdated,
    "dedup_hit": DedupHit,
    "stopped": Stopped,
    "remeasure_ready": RemeasureReady,
}


class Event(FrozenModel):
    """One state change, stored at events/<actor>/<local_id>.json and never modified."""

    seq: int | None = None
    local_id: str
    ts: float
    actor: ActorId
    role: Role
    type: EventType
    target: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    source: str | None = None
    reason: str | None = None
    evidence: tuple[str, ...] = ()
    data: EventPayload | None = None

    @field_validator("data", mode="before")
    @classmethod
    def select_payload_model(cls, value: object, info: ValidationInfo) -> object:
        event_type = info.data.get("type")
        if value is None:
            return None
        elif isinstance(event_type, str) and event_type in PAYLOAD_MODELS:
            return PAYLOAD_MODELS[event_type].model_validate(value)
        else:
            raise ValueError(f"events of type {event_type!r} carry no data")

    @model_validator(mode="after")
    def require_payload_for_type(self) -> Self:
        expected_model = PAYLOAD_MODELS.get(self.type)
        if expected_model is None:
            return self
        elif not isinstance(self.data, expected_model):
            raise ValueError(
                f"a {self.type} event needs a {expected_model.__name__} payload"
            )
        else:
            return self


def event_to_json(event: Event) -> str:
    """The one serialization path for events; keys use wire names such as "from"."""
    return event.model_dump_json()


def event_from_json(text: str) -> Event:
    return Event.model_validate_json(text)
