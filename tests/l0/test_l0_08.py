"""L0-08 (v2.2 §14.1). Owner: W1-03 (#13).

lease TTL expires → the task goes back to the queue once and only once (D32).
"""

import pytest

from tests.fixtures.herd import make_store_blackboard
from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.core.queue.queue import KeyValueLeases, KeyValueTaskQueue
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task

pytestmark = [pytest.mark.l0, pytest.mark.queue]

LEASE_TTL_SECONDS = 7200.0


def test_l0_08_stale_lease_requeues_once() -> None:
    """lease TTL expires → the task goes back to the queue once and only once (D32)."""
    clock = FakeClock()
    store = MemoryKeyValueStore(clock=clock, namespace="run-1")
    blackboard = make_store_blackboard(clock)
    queue = KeyValueTaskQueue(store=store)
    two_reclaimers = [
        KeyValueLeases(
            store=store,
            task_queue=queue,
            blackboard=blackboard,
            clock=clock,
            lease_ttl_seconds=LEASE_TTL_SECONDS,
        )
        for _ in range(2)
    ]
    task = Task(
        id="t-00001",
        approach="a-01",
        kind="advance",
        goal="Advance the whole problem using approach a-01.",
        created_by="queue",
    )
    queue.put(task)
    claimed = queue.claim_task(ActorId("worker-01"))
    assert claimed is not None
    two_reclaimers[0].acquire(ActorId("worker-01"), claimed, "handle-1")
    clock.advance(LEASE_TTL_SECONDS + 1.0)

    requeued = [
        task_id
        for reclaimer in two_reclaimers
        for task_id in reclaimer.reclaim_stale(
            clock.now_seconds(), lambda handle_id: False
        )
    ]
    assert requeued == ["t-00001"]
    assert [entry[0] for entry in store.sorted_range("queue")] == ["t-00001"]
    assert two_reclaimers[1].get(ActorId("worker-01")) is None
    assert blackboard.list_paths("events/queue/", None) == ("events/queue/00001.json",)
