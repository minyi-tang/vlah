"""L0-05a (v2.2 §14.1). Owner: W1-03 (#13).

result_ready → the follow-up task's for_agent is the submitter (D28).
"""

import pytest

from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.core.queue.queue import KeyValueTaskQueue
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import Event, ResultReady
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import Paired

pytestmark = [pytest.mark.l0, pytest.mark.queue]


def test_l0_05a_result_follow_up_goes_to_submitter() -> None:
    """result_ready → the follow-up task's for_agent is the submitter (D28)."""
    queue = KeyValueTaskQueue(
        store=MemoryKeyValueStore(clock=FakeClock(), namespace="run-1")
    )
    event = Event(
        seq=7,
        local_id="eval_service/00003",
        ts=1.0,
        actor=ActorId("eval_service"),
        role="eval_service",
        type="result_ready",
        data=ResultReady(
            candidate="9f3a",
            claim_id="c-eval_service-00001",
            approach="a-03",
            submitter=ActorId("worker-07"),
            submitter_checkpoint=None,
            baseline="baseline@0",
            paired=Paired(mean=1.8, se=0.6, n_items=5, n_reps=1),
            per_item="candidates/9f3a/result.json",
            score=63.4,
            level="bench_full",
        ),
    )
    follow_up = queue.on_result_ready(event)
    assert follow_up.for_agent == "worker-07"
    assert follow_up.priority == 0
    assert follow_up.approach == "a-03"
    assert follow_up.refs == ("c-eval_service-00001", "9f3a")
    assert (
        queue.on_result_ready(event) == follow_up
    ), "one follow-up per event, even if replayed"
    claimed = queue.claim_task(ActorId("worker-07"))
    assert claimed == follow_up
