"""The task queue and leases in the run's key-value store (v2.2 D1, D28, D32, D55).

Tasks are goals, never "read X" (D1). claim_task orders by: tasks addressed to the
caller, then priority, then the approach furthest below its slot count, then first
in. A task addressed to another agent is open to everyone once that agent holds no
lease. Every contended step is a compare-and-set or an exactly-once removal, so
concurrent claimers and reclaimers never double up.

Compose work runs outside every arm (D74), so it lives in its own queue,
constructed with pool="external".
"""

import logging
from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import TypeAdapter

from vlah.core.blackboard.api import Blackboard
from vlah.core.queue.api import HandleLiveness, TaskQueue
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event, ResultReady, event_to_json
from vlah.core.types.ids import ActorId, format_event_local_id, format_task_id
from vlah.core.types.tasks import Lease, Task
from vlah.ports.store import KeyValueStore

logger = logging.getLogger(__name__)

QUEUE_KEY = "queue"
SLOTS_KEY = "slots"
TASK_KEY_PREFIX = "task/"
LEASE_KEY_PREFIX = "lease/"
DONE_KEY_PREFIX = "done/"
FOLLOW_UP_KEY_PREFIX = "followup/"
RECLAIM_KEY_PREFIX = "reclaimed/"
TASK_COUNTER_KEY = "counter/task"
ENQUEUE_COUNTER_KEY = "counter/enqueue"
EVENT_COUNTER_KEY = "counter/event"
PRIORITY_SCALE = 1e12
QUEUE_ACTOR = ActorId("queue")
SLOTS_ADAPTER = TypeAdapter(dict[str, int])

ADVANCE_GOAL = "Advance the whole problem using approach {approach}."
EXPLORATION_GOAL = (
    "Explore beyond the current approaches: a new direction, a long-tail one, "
    "or a counterexample to a known dead end."
)
FOLLOW_UP_GOAL = (
    "Decide the next step from the result of candidate {candidate} (claim {claim})."
)


def lease_key(agent: ActorId) -> str:
    return f"{LEASE_KEY_PREFIX}{agent}"


def read_leases(store: KeyValueStore) -> tuple[Lease, ...]:
    leases: list[Lease] = []
    for key in store.scan(LEASE_KEY_PREFIX):
        raw_lease = store.get(key)
        if raw_lease is None:
            continue
        else:
            leases.append(Lease.model_validate_json(raw_lease))
    return tuple(leases)


class KeyValueTaskQueue:
    """The task queue; lease counts per approach come from the same store."""

    def __init__(
        self, *, store: KeyValueStore, pool: Literal["arm", "external"] = "arm"
    ) -> None:
        self.store = store
        self.pool = pool

    def load_task(self, task_id: str) -> Task | None:
        raw_task = self.store.get(f"{TASK_KEY_PREFIX}{task_id}")
        if raw_task is None:
            return None
        else:
            return Task.model_validate_json(raw_task)

    def enqueue(self, task: Task) -> None:
        enqueue_order = self.store.increment(ENQUEUE_COUNTER_KEY, 1)
        self.store.sorted_add(
            QUEUE_KEY, task.id, task.priority * PRIORITY_SCALE + enqueue_order
        )

    def put(self, task: Task) -> None:
        if task.pool != self.pool:
            raise ValueError(
                f"a {task.pool}-pool task cannot go in the {self.pool} queue"
            )
        elif not self.store.compare_and_set(
            f"{TASK_KEY_PREFIX}{task.id}", None, task.model_dump_json().encode(), None
        ):
            raise ValueError(f"task {task.id} already exists")
        else:
            self.enqueue(task)

    def queued_tasks(self) -> list[tuple[Task, float]]:
        queued: list[tuple[Task, float]] = []
        for task_id, score in self.store.sorted_range(QUEUE_KEY):
            task = self.load_task(task_id)
            if task is None:
                continue
            else:
                queued.append((task, score))
        return queued

    def slots(self) -> dict[str, int]:
        raw_slots = self.store.get(SLOTS_KEY)
        if raw_slots is None:
            return {}
        else:
            return SLOTS_ADAPTER.validate_json(raw_slots)

    def claim_task(self, agent: ActorId) -> Task | None:
        leases = read_leases(self.store)
        lease_holders = {lease.agent for lease in leases}
        active_by_approach: dict[str, int] = {}
        for lease in leases:
            active_by_approach[lease.approach] = (
                active_by_approach.get(lease.approach, 0) + 1
            )
        slots = self.slots()

        def claim_order(entry: tuple[Task, float]) -> tuple[int, int, int, float]:
            task, score = entry
            slot_gap = slots.get(task.approach, 0) - active_by_approach.get(
                task.approach, 0
            )
            return (
                0 if task.for_agent == agent else 1,
                task.priority,
                -slot_gap,
                score,
            )

        eligible = [
            (task, score)
            for task, score in self.queued_tasks()
            if task.for_agent is None
            or task.for_agent == agent
            or task.for_agent not in lease_holders
        ]
        for task, score in sorted(eligible, key=claim_order):
            if self.store.sorted_remove(QUEUE_KEY, task.id):
                return task
            else:
                continue
        return None

    def complete(self, task_id: str, agent: ActorId) -> None:
        self.store.set(f"{DONE_KEY_PREFIX}{task_id}", agent.encode(), None)
        self.store.delete(f"{TASK_KEY_PREFIX}{task_id}")

    def requeue(self, task_id: str, reason: str) -> None:
        queued_ids = {entry[0] for entry in self.store.sorted_range(QUEUE_KEY)}
        task = self.load_task(task_id)
        if self.store.get(f"{DONE_KEY_PREFIX}{task_id}") is not None or task is None:
            logger.info(f"not requeueing {task_id}: it is already done")
        elif task_id in queued_ids:
            logger.info(f"not requeueing {task_id}: it is already queued")
        else:
            logger.info(f"requeueing {task_id}: {reason}")
            self.enqueue(task)

    def next_task_id(self) -> str:
        return format_task_id(self.store.increment(TASK_COUNTER_KEY, 1))

    def maintain(self, slots_per_approach: Mapping[str, int]) -> tuple[Task, ...]:
        self.store.set(
            SLOTS_KEY, SLOTS_ADAPTER.dump_json(dict(slots_per_approach)), None
        )
        leases = read_leases(self.store)
        queued = [entry[0] for entry in self.queued_tasks()]
        added: list[Task] = []
        for approach, slot_count in sorted(slots_per_approach.items()):
            active_count = sum(1 for lease in leases if lease.approach == approach)
            queued_count = sum(
                1
                for task in queued
                if task.approach == approach and task.kind == "advance"
            )
            for _ in range(slot_count - active_count - queued_count):
                task = Task(
                    id=self.next_task_id(),
                    approach=approach,
                    kind="advance",
                    goal=ADVANCE_GOAL.format(approach=approach),
                    created_by="queue",
                )
                self.put(task)
                added.append(task)
        if not added and not queued and slots_per_approach:
            least_staffed = min(
                sorted(slots_per_approach),
                key=lambda approach: sum(
                    1 for lease in leases if lease.approach == approach
                ),
            )
            exploration = Task(
                id=self.next_task_id(),
                approach=least_staffed,
                kind="exploration",
                goal=EXPLORATION_GOAL,
                created_by="queue",
            )
            self.put(exploration)
            added.append(exploration)
        else:
            pass
        return tuple(added)

    def on_result_ready(self, event: Event) -> Task:
        """One follow-up per result_ready, keyed by the event's sequence number."""
        if event.type != "result_ready" or not isinstance(event.data, ResultReady):
            raise ValueError(f"not a result_ready event: {event.type}")
        elif event.seq is None:
            raise ValueError(
                "on_result_ready needs a merged event with a sequence number"
            )
        else:
            follow_up_key = f"{FOLLOW_UP_KEY_PREFIX}{event.seq}"
            existing_task_id = self.store.get(follow_up_key)
            if existing_task_id is not None:
                existing = self.load_task(existing_task_id.decode())
                assert existing is not None
                return existing
            else:
                result = event.data
                task = Task(
                    id=self.next_task_id(),
                    approach=result.approach,
                    kind="result_followup",
                    goal=FOLLOW_UP_GOAL.format(
                        candidate=result.candidate, claim=result.claim_id
                    ),
                    refs=(result.claim_id, result.candidate),
                    priority=0,
                    for_agent=result.submitter,
                    created_by="queue",
                )
                if self.store.compare_and_set(
                    follow_up_key, None, task.id.encode(), None
                ):
                    self.put(task)
                    return task
                else:
                    return self.on_result_ready(event)


class LeaseTransform(Protocol):
    def __call__(self, lease: Lease) -> Lease: ...


class KeyValueLeases:
    """Leases keyed by agent; the executor's sidecar refreshes them through beat."""

    def __init__(
        self,
        *,
        store: KeyValueStore,
        task_queue: TaskQueue,
        blackboard: Blackboard,
        clock: Clock,
        lease_ttl_seconds: float,
    ) -> None:
        self.store = store
        self.task_queue = task_queue
        self.blackboard = blackboard
        self.clock = clock
        self.lease_ttl_seconds = lease_ttl_seconds

    def acquire(self, agent: ActorId, task: Task, handle_id: str) -> Lease:
        now_seconds = self.clock.now_seconds()
        lease = Lease(
            agent=agent,
            approach=task.approach,
            task_id=task.id,
            handle_id=handle_id,
            target_claims=task.refs,
            heartbeat_ts=now_seconds,
            acquired_ts=now_seconds,
        )
        if self.store.compare_and_set(
            lease_key(agent), None, lease.model_dump_json().encode(), None
        ):
            return lease
        else:
            raise ValueError(f"{agent} already holds a lease")

    def update_lease(self, agent: ActorId, transform: LeaseTransform) -> None:
        """Apply transform with compare-and-set, retrying if another writer got there first."""
        while True:
            raw_lease = self.store.get(lease_key(agent))
            if raw_lease is None:
                return
            else:
                updated = transform(Lease.model_validate_json(raw_lease))
                if self.store.compare_and_set(
                    lease_key(agent),
                    raw_lease,
                    updated.model_dump_json().encode(),
                    None,
                ):
                    return
                else:
                    continue

    def beat(
        self, agent: ActorId, files_touched: tuple[str, ...], timestamp_seconds: float
    ) -> None:
        self.update_lease(
            agent,
            lambda lease: lease.model_copy(
                update={
                    "heartbeat_ts": timestamp_seconds,
                    "files_touched": files_touched,
                }
            ),
        )

    def add_targets(
        self, agent: ActorId, claim_ids: tuple[str, ...], depends_on: tuple[str, ...]
    ) -> None:
        if self.get(agent) is None:
            raise ValueError(f"{agent} holds no lease")
        else:
            self.update_lease(
                agent,
                lambda lease: lease.model_copy(
                    update={
                        "target_claims": tuple(
                            dict.fromkeys(lease.target_claims + claim_ids)
                        ),
                        "depends_on": tuple(
                            dict.fromkeys(lease.depends_on + depends_on)
                        ),
                    }
                ),
            )

    def release(self, agent: ActorId) -> None:
        self.store.delete(lease_key(agent))

    def get(self, agent: ActorId) -> Lease | None:
        raw_lease = self.store.get(lease_key(agent))
        if raw_lease is None:
            return None
        else:
            return Lease.model_validate_json(raw_lease)

    def active(self) -> tuple[Lease, ...]:
        return read_leases(self.store)

    def reclaim_stale(
        self, now_seconds: float, liveness: HandleLiveness
    ) -> tuple[str, ...]:
        requeued: list[str] = []
        for lease in self.active():
            if lease.heartbeat_ts + self.lease_ttl_seconds > now_seconds:
                continue
            elif liveness(lease.handle_id):
                # Heartbeats are late but the process lives: never reclaim a live worker.
                continue
            elif not self.store.compare_and_set(
                f"{RECLAIM_KEY_PREFIX}{lease.task_id}/{lease.acquired_ts!r}",
                None,
                b"1",
                None,
            ):
                continue
            else:
                raw_lease = self.store.get(lease_key(lease.agent))
                if (
                    raw_lease is not None
                    and Lease.model_validate_json(raw_lease) == lease
                ):
                    self.store.delete(lease_key(lease.agent))
                else:
                    pass
                self.task_queue.requeue(
                    lease.task_id, f"no heartbeat from {lease.agent}"
                )
                self.write_stale_event(lease, now_seconds)
                requeued.append(lease.task_id)
        return tuple(requeued)

    def write_stale_event(self, lease: Lease, now_seconds: float) -> None:
        event = Event(
            local_id=format_event_local_id(
                QUEUE_ACTOR, self.store.increment(EVENT_COUNTER_KEY, 1)
            ),
            ts=now_seconds,
            actor=QUEUE_ACTOR,
            role="indexer",
            type="lease_stale",
            target=lease.task_id,
            reason=f"no heartbeat from {lease.agent} since {lease.heartbeat_ts}",
        )
        receipt = self.blackboard.write(
            QUEUE_ACTOR,
            {f"events/{event.local_id}.json": event_to_json(event).encode()},
            f"lease_stale for {lease.task_id}",
        )
        if receipt.status == "rejected":
            raise RuntimeError(
                f"the lease_stale event was rejected: {receipt.violations}"
            )
        else:
            pass
