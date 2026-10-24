"""Task queue and leases (v2.2 D28, D32, D55), on the memory store, SQLite and Redis."""

import threading
from dataclasses import dataclass
from pathlib import Path

import fakeredis
import pytest

from tests.fixtures.herd import make_store_blackboard
from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.adapters.store.redis.store import RedisKeyValueStore
from vlah.adapters.store.sqlite.store import SqliteKeyValueStore
from vlah.core.queue.queue import KeyValueLeases, KeyValueTaskQueue
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import Event, ResultReady
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import Paired
from vlah.core.types.tasks import Task
from vlah.ports.store import KeyValueStore
from vlah.testing.conformance import check_key_value_store

pytestmark = pytest.mark.queue

LEASE_TTL_SECONDS = 7200.0
WORKER_01 = ActorId("worker-01")
WORKER_02 = ActorId("worker-02")


@dataclass(frozen=True, kw_only=True)
class StoreChoice:
    kind: str
    directory: Path


def make_store(choice: StoreChoice, clock: FakeClock) -> KeyValueStore:
    if choice.kind == "memory":
        return MemoryKeyValueStore(clock=clock, namespace="run-1")
    elif choice.kind == "sqlite":
        return SqliteKeyValueStore(
            path=choice.directory / "state.sqlite", clock=clock, namespace="run-1"
        )
    else:
        return RedisKeyValueStore(
            client=fakeredis.FakeRedis(), clock=clock, namespace="run-1"
        )


@pytest.fixture(params=["memory", "sqlite", "redis"])
def store_kind(request: pytest.FixtureRequest, tmp_path: Path) -> StoreChoice:
    return StoreChoice(kind=str(request.param), directory=tmp_path)


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


def test_sqlite_store_passes_the_key_value_check(tmp_path: Path) -> None:
    clock = FakeClock()

    class ClockTimer:
        def wait_seconds(self, seconds: float) -> None:
            clock.advance(seconds)

    check_key_value_store(
        SqliteKeyValueStore(
            path=tmp_path / "state.sqlite", clock=clock, namespace="check"
        ),
        ClockTimer(),
    )


def test_sqlite_state_survives_a_crash_and_stays_in_its_namespace(
    tmp_path: Path,
) -> None:
    """A resumed run finds its counters, queue and leases as the crashed process left
    them, TTLs included, and no other run's keys (#120)."""
    clock = FakeClock()
    path = tmp_path / "state.sqlite"
    crashed = SqliteKeyValueStore(path=path, clock=clock, namespace="r-1")
    crashed.increment("counter/task", 3)
    crashed.sorted_add("queue", "t-00002", 2.0)
    crashed.set("lease/worker-01", b"lease", 10.0)
    SqliteKeyValueStore(path=path, clock=clock, namespace="r-2").set("x", b"x", None)
    resumed = SqliteKeyValueStore(path=path, clock=clock, namespace="r-1")
    assert resumed.increment("counter/task", 1) == 4
    assert resumed.sorted_range("queue") == (("t-00002", 2.0),)
    assert resumed.scan("") == ("counter/task", "lease/worker-01")
    clock.advance(10.0)
    assert resumed.get("lease/worker-01") is None, "the TTL runs from the first write"


def test_concurrent_increments_never_lose_a_count(store_kind: StoreChoice) -> None:
    store = make_store(store_kind, FakeClock())
    thread_count = 8
    increments_per_thread = 25
    barrier = threading.Barrier(thread_count, timeout=10)
    returned: list[int] = []
    returned_lock = threading.Lock()

    def count() -> None:
        barrier.wait()
        for _ in range(increments_per_thread):
            value = store.increment("counter/task", 1)
            with returned_lock:
                returned.append(value)

    threads = [threading.Thread(target=count) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
    total = thread_count * increments_per_thread
    assert sorted(returned) == list(range(1, total + 1)), "every caller got its own"


def test_a_task_for_another_agent_opens_up_once_that_agent_has_no_lease(
    store_kind: StoreChoice,
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


def test_external_tasks_never_reach_arm_workers(store_kind: StoreChoice) -> None:
    """Compose work runs outside every arm (D74)."""
    store = make_store(store_kind, FakeClock())
    arm_queue = KeyValueTaskQueue(store=store)
    external_queue = KeyValueTaskQueue(store=store, pool="external")
    compose_task = make_task("t-compose", kind="rebase_compose", pool="external")
    with pytest.raises(ValueError, match="external-pool"):
        arm_queue.put(compose_task)
    external_queue.put(compose_task)
    assert arm_queue.claim_task(WORKER_01) is None, "one store, separate queues"
    assert [task.kind for task in arm_queue.maintain({"a-01": 0})] == ["exploration"]
    claimed = external_queue.claim_task(ActorId("worker-99"))
    assert claimed is not None and claimed.id == "t-compose"


def test_concurrent_claimers_never_get_the_same_task(store_kind: StoreChoice) -> None:
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


def test_heartbeats_and_live_handles_both_prevent_a_reclaim(
    store_kind: StoreChoice,
) -> None:
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
    store_kind: StoreChoice,
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


def make_leases(store: KeyValueStore, clock: FakeClock) -> KeyValueLeases:
    return KeyValueLeases(
        store=store,
        task_queue=KeyValueTaskQueue(store=store),
        blackboard=make_store_blackboard(clock),
        clock=clock,
        lease_ttl_seconds=LEASE_TTL_SECONDS,
    )


def stale_lease(
    store_kind: StoreChoice,
) -> tuple[FakeClock, KeyValueStore, KeyValueLeases]:
    clock = FakeClock()
    store = make_store(store_kind, clock)
    leases = make_leases(store, clock)
    queue = KeyValueTaskQueue(store=store)
    queue.put(make_task("t-1"))
    claimed = queue.claim_task(WORKER_01)
    assert claimed is not None
    leases.acquire(WORKER_01, claimed, "handle-1")
    clock.advance(LEASE_TTL_SECONDS + 1.0)
    return clock, store, leases


def test_a_heartbeat_during_the_liveness_check_keeps_the_lease(
    store_kind: StoreChoice,
) -> None:
    """Two leases on one task must never happen (#42, B6)."""
    clock, store, leases = stale_lease(store_kind)

    def late_beat(handle_id: str) -> bool:
        leases.beat(WORKER_01, ("x.py",), clock.now_seconds())
        return False

    assert leases.reclaim_stale(clock.now_seconds(), late_beat) == ()
    assert leases.get(WORKER_01) is not None
    assert KeyValueTaskQueue(store=store).claim_task(WORKER_02) is None


def test_concurrent_reclaimers_requeue_a_stale_task_exactly_once(
    store_kind: StoreChoice,
) -> None:
    clock, store, leases = stale_lease(store_kind)
    barrier = threading.Barrier(4, timeout=10)
    requeued: list[tuple[str, ...]] = []

    def dead_after_everyone_asked(handle_id: str) -> bool:
        barrier.wait()
        return False

    def reclaim() -> None:
        requeued.append(
            leases.reclaim_stale(clock.now_seconds(), dead_after_everyone_asked)
        )

    threads = [threading.Thread(target=reclaim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
    assert sorted(requeued) == [(), (), (), ("t-1",)]
    assert leases.get(WORKER_01) is None
    assert [entry[0] for entry in store.sorted_range("queue")] == ["t-1"]
    again = KeyValueTaskQueue(store=store).claim_task(WORKER_01)
    assert again is not None
    leases.acquire(WORKER_01, again, "handle-2")
    assert leases.get(WORKER_01) is not None, "a reclaimed agent can lease again"


def test_a_task_still_leased_is_not_requeued(store_kind: StoreChoice) -> None:
    clock = FakeClock()
    store = make_store(store_kind, clock)
    queue = KeyValueTaskQueue(store=store)
    leases = make_leases(store, clock)
    queue.put(make_task("t-1"))
    claimed = queue.claim_task(WORKER_01)
    assert claimed is not None
    leases.acquire(WORKER_01, claimed, "handle-1")
    queue.requeue("t-1", "the router asked")
    assert queue.claim_task(WORKER_02) is None
    leases.release(WORKER_01)
    queue.requeue("t-1", "released, then requeued")
    assert queue.claim_task(WORKER_02) is not None


def result_ready_event(submitter: ActorId) -> Event:
    return Event(
        seq=7,
        local_id="eval_service/00003",
        ts=1.0,
        actor=ActorId("eval_service"),
        role="eval_service",
        type="result_ready",
        data=ResultReady(
            candidate="9f3a",
            claim_id="c-eval_service-00001",
            approach="a-01",
            submitter=submitter,
            submitter_checkpoint=None,
            baseline="baseline@0",
            paired=Paired(mean=1.8, se=0.6, n_items=5, n_reps=1),
            per_item="candidates/9f3a/result.json",
            score=63.4,
            level="slow",
        ),
    )


def test_replaying_a_result_after_its_follow_up_completed(
    store_kind: StoreChoice,
) -> None:
    """A rebuild re-feeds every result_ready; it must not raise in the merge fan-out (S5)."""
    store = make_store(store_kind, FakeClock())
    queue = KeyValueTaskQueue(store=store)
    event = result_ready_event(WORKER_01)
    follow_up = queue.on_result_ready(event)
    claimed = queue.claim_task(WORKER_01)
    assert claimed is not None and claimed.id == follow_up.id
    queue.complete(claimed.id, WORKER_01)
    assert queue.on_result_ready(event).id == follow_up.id
    assert queue.claim_task(WORKER_01) is None, "the replay queues nothing"


def test_an_external_queues_follow_up_stays_out_of_the_arm_pool(
    store_kind: StoreChoice,
) -> None:
    """The follow-up used to be built in the arm pool whichever queue made it, so the
    external queue refused its own follow-up (#56 R2)."""
    store = make_store(store_kind, FakeClock())
    arm_queue = KeyValueTaskQueue(store=store)
    external_queue = KeyValueTaskQueue(store=store, pool="external")
    compose_agent = ActorId("worker-00")
    follow_up = external_queue.on_result_ready(result_ready_event(compose_agent))
    assert follow_up.pool == "external"
    assert arm_queue.claim_task(WORKER_01) is None
    claimed = external_queue.claim_task(compose_agent)
    assert claimed is not None and claimed.id == follow_up.id


def test_scan_takes_glob_characters_literally(store_kind: StoreChoice) -> None:
    store = make_store(store_kind, FakeClock())
    store.set("lease/worker-[1]", b"x", None)
    store.set("lease/worker-1", b"x", None)
    assert store.scan("lease/worker-[1]") == ("lease/worker-[1]",)
