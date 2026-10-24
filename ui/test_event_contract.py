"""Every current canonical Event type must survive recording without field loss."""

import json
from typing import get_args

from pydantic import JsonValue

from tests.fixtures.herd import make_store_blackboard
from ui.replay_recording import TraceRecorder
from vlah.adapters.memory.model import MemoryModel
from vlah.adapters.memory.store import MemoryVectorIndex
from vlah.core.blackboard.api import MergedCommit
from vlah.core.indexer.indexer import StoreIndexer
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import PAYLOAD_MODELS, Event, EventType, event_to_json
from vlah.core.types.ids import ActorId, format_event_local_id
from vlah.core.types.params import Params


def test_every_canonical_event_is_preserved_and_duplicate_delivery_is_idempotent() -> (
    None
):
    paired: dict[str, JsonValue] = {"mean": 1.0, "se": 0.1, "n_items": 5, "n_reps": 1}
    payloads: dict[str, dict[str, JsonValue]] = {
        "result_ready": {
            "candidate": "ab" * 32,
            "claim_id": "c-eval_service-00001",
            "approach": "a-01",
            "submitter": "worker-01",
            "submitter_checkpoint": None,
            "baseline": "baseline@0",
            "paired": paired,
            "per_item": "candidates/result.json",
            "score": 11.0,
            "level": "slow",
            "constraint_ok": True,
        },
        "baseline_advanced": {
            "baseline": 1,
            "parent_baseline": 0,
            "candidate": "ab" * 32,
            "files_changed": ["policy.py"],
            "paired": paired,
            "guard_pending": True,
        },
        "baseline_reverted": {
            "from_baseline": 1,
            "new_baseline": 2,
            "removed": ["ab" * 32],
            "reason": "guard",
        },
        "integration_failed": {
            "candidate": "ab" * 32,
            "baseline": 0,
            "paired": paired,
            "reason": "no gain",
        },
        "interaction_found": {
            "candidate": "ab" * 32,
            "baseline": 1,
            "earlier": paired,
            "remeasured": paired,
            "overlaps_with": ["cd" * 32],
        },
        "audit_checked": {
            "segment": [0, 1],
            "daily": paired,
            "audit": paired,
            "threshold": 1.0,
            "flagged": False,
        },
        "map_updated": {"baseline": 1, "measurements": ["gap"], "output": "map/gap.md"},
        "dedup_hit": {"candidate": "ab" * 32, "original_claim": "c-eval_service-00001"},
        "stopped": {"reason": "human"},
        "remeasure_ready": {
            "request_id": "remeasure-1",
            "candidate": "ab" * 32,
            "parent_candidate": "baseline@0",
            "claim_id": "c-eval_service-00001",
            "baseline": "baseline@0",
            "paired": paired,
            "score": 11.0,
            "level": "slow",
            "constraint_ok": True,
            "per_seed": [10.8, 11.2],
            "dataset": "audit",
        },
    }
    assert set(payloads) == set(PAYLOAD_MODELS)
    clock = FakeClock()
    blackboard = make_store_blackboard(clock)
    indexer = StoreIndexer(
        blackboard=blackboard,
        vectors=MemoryVectorIndex(),
        model=MemoryModel(),
        params=Params(),
        clock=clock,
        top_level=None,
    )
    recorder = TraceRecorder(blackboard, indexer, clock)
    for sequence, kind in enumerate(get_args(EventType), start=1):
        clock.advance(1)
        event = Event.model_validate(
            {
                "seq": sequence,
                "local_id": format_event_local_id(ActorId("integrator"), sequence),
                "ts": clock.now_seconds(),
                "actor": "integrator",
                "role": "integrator",
                "type": kind,
                "target": "a-01",
                "from": "before",
                "to": "after",
                "source": "test-source",
                "reason": "Recorded reason.",
                "evidence": ["claims/worker-01/c-worker-01-00001.md"],
                "data": payloads.get(kind),
            }
        )
        commit = MergedCommit(
            commit_id=str(sequence),
            actor=ActorId("integrator"),
            paths=(),
            events=(event,),
            claims=(),
        )
        recorder(commit)
        assert recorder.events[-1].canonical == json.loads(event_to_json(event))
        count = len(recorder.events)
        recorder(commit)
        assert len(recorder.events) == count
    assert {event.kind for event in recorder.events} == set(get_args(EventType))
