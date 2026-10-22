"""Events choose their payload class by type and serialize with wire names (v2.2 D56)."""

import pytest
from pydantic import ValidationError

from vlah.core.types.events import (
    PAYLOAD_MODELS,
    AuditChecked,
    BaselineAdvanced,
    BaselineReverted,
    DedupHit,
    Event,
    EventPayload,
    IntegrationFailed,
    InteractionFound,
    MapUpdated,
    RemeasureReady,
    ResultReady,
    Stopped,
    event_from_json,
    event_to_json,
)
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import Paired

PAIRED = Paired(mean=1.8, se=0.6, n_items=120, n_reps=1)

EXAMPLE_PAYLOADS: dict[str, EventPayload] = {
    "result_ready": ResultReady(
        candidate="9f3a",
        claim_id="c-eval_service-00001",
        approach="a-03",
        submitter=ActorId("worker-07"),
        submitter_checkpoint=None,
        baseline="baseline@6",
        paired=PAIRED,
        per_item="eval/results/9f3a/per_item.json",
        score=63.4,
        level="bench_full",
    ),
    "baseline_advanced": BaselineAdvanced(
        baseline=7,
        parent_baseline=6,
        candidate="9f3a",
        files_changed=("prompt_builder.py",),
        paired=PAIRED,
        guard_pending=True,
    ),
    "baseline_reverted": BaselineReverted(
        from_baseline=9, new_baseline=10, removed=("9f3a",), reason="audit"
    ),
    "integration_failed": IntegrationFailed(
        candidate="9f3a", baseline=6, paired=PAIRED, reason="below_1se"
    ),
    "interaction_found": InteractionFound(
        candidate="9f3a",
        baseline=6,
        earlier=PAIRED,
        remeasured=PAIRED,
        overlaps_with=("77be",),
    ),
    "audit_checked": AuditChecked(
        segment=(3, 5), daily=PAIRED, audit=PAIRED, threshold=2.1, flagged=False
    ),
    "map_updated": MapUpdated(
        baseline=7, measurements=("wait_by_site",), output="map/measurements/x/7/"
    ),
    "dedup_hit": DedupHit(candidate="9f3a", original_claim="c-eval_service-00001"),
    "stopped": Stopped(reason="budget"),
    "remeasure_ready": RemeasureReady(
        request_id="bench_full-00002",
        candidate="77be",
        parent_candidate="9f3a",
        claim_id="c-eval_service-00004",
        baseline="baseline@7",
        paired=PAIRED,
        score=64.1,
        level="bench_full",
    ),
}


def make_event(event_type: str, payload: EventPayload | None) -> Event:
    return Event.model_validate(
        {
            "local_id": "worker-07/00001",
            "ts": 1.0,
            "actor": "worker-07",
            "role": "worker",
            "type": event_type,
            "from": "proposed",
            "to": "refuted",
            "data": None if payload is None else payload.model_dump(),
        }
    )


def test_examples_cover_every_payload_type() -> None:
    assert set(EXAMPLE_PAYLOADS) == set(PAYLOAD_MODELS)


@pytest.mark.parametrize("event_type", sorted(PAYLOAD_MODELS))
def test_payload_round_trips_with_its_own_class(event_type: str) -> None:
    """Payloads share field names, so a guessed union member would be silently wrong."""
    event = make_event(event_type, EXAMPLE_PAYLOADS[event_type])
    text = event_to_json(event)
    parsed = event_from_json(text)
    assert '"from":"proposed"' in text
    assert parsed == event
    assert type(parsed.data) is PAYLOAD_MODELS[event_type]


def test_payload_is_required_where_the_type_defines_one() -> None:
    with pytest.raises(ValidationError):
        make_event("result_ready", None)


def test_payload_is_rejected_where_the_type_defines_none() -> None:
    with pytest.raises(ValidationError):
        make_event("claim_status", EXAMPLE_PAYLOADS["dedup_hit"])
