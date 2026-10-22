"""L0-01 (v2.2 §14.1). Owner: W1-03 (#13).

claim_task selection order: for_agent first, then priority, then the approach with the largest target_slots gap (D55).
"""

import pytest

from tests.fixtures.herd import make_store_blackboard
from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.core.queue.queue import KeyValueLeases, KeyValueTaskQueue
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task

pytestmark = [pytest.mark.l0, pytest.mark.queue]


def task(
    task_id: str, approach: str, priority: int, for_agent: str | None = None
) -> Task:
    return Task.model_validate(
        {
            "id": task_id,
            "approach": approach,
            "kind": "advance",
            "goal": f"Advance the whole problem using approach {approach}.",
            "priority": priority,
            "for_agent": for_agent,
            "created_by": "queue",
        }
    )


def test_l0_01_claim_task_order() -> None:
    """claim_task selection order: for_agent first, then priority, then the approach with the largest target_slots gap (D55)."""
    clock = FakeClock()
    store = MemoryKeyValueStore(clock=clock, namespace="run-1")
    queue = KeyValueTaskQueue(store=store)
    leases = KeyValueLeases(
        store=store,
        task_queue=queue,
        blackboard=make_store_blackboard(clock),
        clock=clock,
        lease_ttl_seconds=7200.0,
    )
    queue.maintain({"a-01": 3, "a-02": 1})
    for queued in queue.queued_tasks():
        store.sorted_remove("queue", queued[0].id)
    # a-02's task goes in first, so first-in-first-out alone would pick it: only the
    # slot-gap rule picks a-01's.
    queue.put(task("t-a02-low", "a-02", priority=2))
    queue.put(task("t-a01-low", "a-01", priority=2))
    queue.put(task("t-a01-high", "a-01", priority=1))
    queue.put(task("t-for-02", "a-02", priority=2, for_agent="worker-02"))

    first = queue.claim_task(ActorId("worker-02"))
    assert first is not None and first.id == "t-for-02", "for_agent comes first"
    leases.acquire(ActorId("worker-02"), first, "handle-2")

    second = queue.claim_task(ActorId("worker-01"))
    assert second is not None and second.id == "t-a01-high", "then priority"
    leases.acquire(ActorId("worker-01"), second, "handle-1")

    third = queue.claim_task(ActorId("worker-03"))
    assert (
        third is not None and third.id == "t-a01-low"
    ), "then the largest slot gap: a-01 has 3 - 1 = 2 free, a-02 has 1 - 1 = 0"
