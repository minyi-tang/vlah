"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

The task queue and leases, kept in the key-value store (v2.2 D28, D32, D55).
Implemented by W1-03 (#13).
"""

from collections.abc import Mapping
from typing import Protocol

from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Lease, Task


class HandleLiveness(Protocol):
    def __call__(self, handle_id: str) -> bool:
        """True while the executor still runs this handle."""
        ...


class TaskQueue(Protocol):
    def put(self, task: Task) -> None: ...

    def claim_task(self, agent: ActorId) -> Task | None:
        """for_agent first, then priority, then the approach furthest below its slots (D55)."""
        ...

    def complete(self, task_id: str, agent: ActorId) -> None: ...

    def requeue(self, task_id: str, reason: str) -> None: ...

    def maintain(self, slots_per_approach: Mapping[str, int]) -> tuple[Task, ...]:
        """Top up regular tasks to each approach's slot count; returns the tasks added."""
        ...

    def on_result_ready(self, event: Event) -> Task:
        """The follow-up task for the submitter, priority 0 (D28)."""
        ...


class Leases(Protocol):
    """Also a ports.executor.HeartbeatSink: the executor's sidecar calls beat."""

    def acquire(self, agent: ActorId, task: Task, handle_id: str) -> Lease: ...

    def beat(
        self, agent: ActorId, files_touched: tuple[str, ...], timestamp_seconds: float
    ) -> None: ...

    def add_targets(
        self, agent: ActorId, claim_ids: tuple[str, ...], depends_on: tuple[str, ...]
    ) -> None: ...

    def release(self, agent: ActorId) -> None: ...

    def get(self, agent: ActorId) -> Lease | None: ...

    def active(self) -> tuple[Lease, ...]: ...

    def reclaim_stale(
        self, now_seconds: float, liveness: HandleLiveness
    ) -> tuple[str, ...]:
        """Requeue each expired lease's task exactly once, after asking the executor first."""
        ...
