"""Task queue and leases (v2.2 D28, D32, D55), on the memory store and on Redis."""

import threading

import fakeredis
import pytest

from tests.fixtures.herd import make_store_blackboard
from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.adapters.store.redis.store import RedisKeyValueStore
from vlah.core.queue.queue import KeyValueLeases, KeyValueTaskQueue
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.ports.store import KeyValueStore
from vlah.testing.conformance import check_key_value_store

pytestmark = pytest.mark.queue

LEASE_TTL_SECONDS = 7200.0
WORKER_01 = ActorId("worker-01")
WORKER_02 = ActorId("worker-02")


def make_store(kind: str, clock: FakeClock) -> KeyValueStore:
    if kind == "memory":
        return MemoryKeyValueStore(clock=clock, namespace="run-1")
    else:
        return RedisKeyValueStore(
            client=fakeredis.FakeRedis(), clock=clock, namespace="run-1"
        )


@pytest.fixture(params=["memory", "redis"])
def store_kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def make_task(task_id: str, approach: str = "a-01", **fields: str) -> Task:
    return Task.model_validate(
        {
            "id": task_id,
            "approach": approach,
            "kind": "advance",
            "goal": f"Advance the whole problem using approach {approach}.",
            "created_by": "queue",
            **fields,
        }
    )


def test_redis_store_passes_the_key_value_check() -> None:
    clock = FakeClock()

    class ClockTimer:
        def wait_seconds(self, seconds: float) -> None:
            clock.advance(seconds)

    check_key_value_store(
        RedisKeyValueStore(
            client=fakeredis.FakeRedis(), clock=clock, namespace="check"
        ),
        ClockTimer(),
    )


def test_a_task_for_another_agent_opens_up_once_that_agent_has_no_lease(
    store_kind: str,
) -> None:
    clock = FakeClock()
    store = make_store(store_kind, clock)
    queue = KeyValueTaskQueue(store=store)
    leases = KeyValueLeases(
        store=store,
        task_queue=queue,
        blackboard=make_store_blackboard(clock),
        clock=clock,
        lease_ttl_seconds=LEASE_TTL_SECONDS,
    )
    leases.acquire(WORKER_01, make_task("t-busy"), "handle-1")
    queue.put(make_task("t-for-1", for_agent=WORKER_01))
    assert queue.claim_task(WORKER_02) is None, "reserved while worker-01 holds a lease"
    leases.release(WORKER_01)
    claimed = queue.claim_task(WORKER_02)
    assert claimed is not None and claimed.id == "t-for-1"


def test_external_tasks_never_reach_arm_workers(store_kind: str) -> None:
    """Compose work runs outside every arm (D74)."""
    store = make_store(store_kind, FakeClock())
    arm_queue = KeyValueTaskQueue(store=store)
    external_queue = KeyValueTaskQueue(
        store=make_store(store_kind, FakeClock()), pool="external"
    )
    compose_task = make_task("t-compose", kind="rebase_compose", pool="external")
    with pytest.raises(ValueError, match="external-pool"):
        arm_queue.put(compose_task)
    external_queue.put(compose_task)
    claimed = external_queue.claim_task(ActorId("worker-99"))
    assert claimed is not None and claimed.id == "t-compose"


def test_concurrent_claimers_never_get_the_same_task(store_kind: str) -> None:
    store = make_store(store_kind, FakeClock())
    queue = KeyValueTaskQueue(store=store)
    for index in range(20):
        queue.put(make_task(f"t-{index:02d}"))
    claimers = [ActorId(f"worker-{index:02d}") for index in range(1, 21)]
    barrier = threading.Barrier(len(claimers))
    claimed: list[str] = []
    claimed_lock = threading.Lock()

    def claim(agent: ActorId) -> None:
        barrier.wait()
        task = queue.claim_task(agent)
        if task is not None:
            with claimed_lock:
                claimed.append(task.id)
        else:
            pass

    threads = [threading.Thread(target=claim, args=(agent,)) for agent in claimers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(claimed) == [f"t-{index:02d}" for index in range(20)]


def test_heartbeats_and_live_handles_both_prevent_a_reclaim(store_kind: str) -> None:
    """A worker in a long sandbox command is alive even if no tool call happens (D32)."""
    clock = FakeClock()
    store = make_store(store_kind, clock)
    queue = KeyValueTaskQueue(store=store)
    leases = KeyValueLeases(
        store=store,
        task_queue=queue,
        blackboard=make_store_blackboard(clock),
        clock=clock,
        lease_ttl_seconds=LEASE_TTL_SECONDS,
    )
    leases.acquire(WORKER_01, make_task("t-long"), "handle-1")
    clock.advance(LEASE_TTL_SECONDS - 30.0)
    leases.beat(WORKER_01, ("policy.py",), clock.now_seconds())
    clock.advance(60.0)
    assert leases.reclaim_stale(clock.now_seconds(), lambda handle_id: False) == ()
    clock.advance(LEASE_TTL_SECONDS)
    assert leases.reclaim_stale(clock.now_seconds(), lambda handle_id: True) == ()
    lease = leases.get(WORKER_01)
    assert lease is not None and lease.files_touched == ("policy.py",)


def test_maintain_counts_running_workers_and_falls_back_to_exploration(
    store_kind: str,
) -> None:
    clock = FakeClock()
    store = make_store(store_kind, clock)
    queue = KeyValueTaskQueue(store=store)
    leases = KeyValueLeases(
        store=store,
        task_queue=queue,
        blackboard=make_store_blackboard(clock),
        clock=clock,
        lease_ttl_seconds=LEASE_TTL_SECONDS,
    )
    leases.acquire(WORKER_01, make_task("t-running"), "handle-1")
    added = queue.maintain({"a-01": 2})
    assert [task.kind for task in added] == ["advance"], "one slot is already running"
    assert queue.maintain({"a-01": 2}) == ()
    assert queue.claim_task(WORKER_02) is not None
    leases.acquire(WORKER_02, make_task("t-second"), "handle-2")
    exploration = queue.maintain({"a-01": 2})
    assert [task.kind for task in exploration] == ["exploration"]
