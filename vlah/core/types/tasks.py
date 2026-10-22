"""Tasks in the queue and leases held by running workers (v2.2 D1, D32, D55)."""

from typing import Literal

from vlah.core.types.base import FrozenModel
from vlah.core.types.ids import ActorId

TaskKind = Literal[
    "advance",
    "result_followup",
    "counterexample",
    "compose_pair",
    "rebase_compose",
    "integration_failed_followup",
    "exploration",
]


class Task(FrozenModel):
    """A goal for one worker; never an instruction to read particular material (D1).

    refs lists the claim and candidate ids the goal is about; they seed the lease's
    target_claims. pool "external" marks compose work run outside every arm (D74).
    """

    id: str
    approach: str
    kind: TaskKind
    goal: str
    refs: tuple[str, ...] = ()
    seed_context: str | None = None
    priority: Literal[0, 1, 2] = 2
    for_agent: ActorId | None = None
    created_by: Literal["queue", "indexer", "human", "scout", "planner", "integrator"]
    pool: Literal["arm", "external"] = "arm"


class Lease(FrozenModel):
    """A running worker's claim on a task, kept in KV and refreshed by the executor."""

    agent: ActorId
    approach: str
    task_id: str
    handle_id: str
    target_claims: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    heartbeat_ts: float
    files_touched: tuple[str, ...] = ()
    acquired_ts: float
