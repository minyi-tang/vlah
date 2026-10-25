"""What a run is built from, and the components an assembly supplies (D58, D67).

build_herd makes one RunContext per run and hands the same instance to the
assembly, so every component it builds sees the same ArmConfig and the same
Params (L0-19). The assembly decides the adapters: in-memory for tests, git and
Redis and subprocesses for real runs. Components that do not exist yet are the
assembly's to supply too, as fakes in tests.
"""

from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Protocol

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard
from vlah.core.budget.api import BudgetMeter
from vlah.core.eval.api import EvalService
from vlah.core.indexer.api import Indexer
from vlah.core.integrator.api import IntegratorService
from vlah.core.metrics.api import Metrics
from vlah.core.problem.api import Problem
from vlah.core.queue.api import Leases, TaskQueue
from vlah.core.router.api import Router
from vlah.core.stop.api import StopRules
from vlah.core.types.clock import Clock
from vlah.core.types.params import Params
from vlah.ports.executor import Executor
from vlah.ports.harness import Harness
from vlah.roles.critic.holdout import HoldoutCritic
from vlah.runtime.isolation import ViewPublisher
from vlah.tools.api import ToolServer


@dataclass(frozen=True, kw_only=True)
class RunContext:
    """One run's identity and settings; the same instance reaches every component."""

    run_id: str
    run_root: Path
    arm: ArmConfig
    params: Params
    problem: Problem
    clock: Clock
    random: Random
    duration_seconds: float


@dataclass(frozen=True, kw_only=True)
class QueuedTaskCounts:
    arm: int
    compose: int


class CountQueuedTasks(Protocol):
    def __call__(self) -> QueuedTaskCounts:
        """Tasks waiting in the arm and compose queues, for the status file.

        The TaskQueue contract has no count, so the assembly that built the queues
        supplies it.
        """
        ...


@dataclass(frozen=True, kw_only=True)
class RunComponents:
    """Every component of one run, built for one RunContext."""

    blackboard: Blackboard
    indexer: Indexer
    task_queue: TaskQueue
    # pool="external": compose tasks, run outside every arm (D74).
    compose_queue: TaskQueue
    leases: Leases
    eval_service: EvalService
    router: Router
    integrator: IntegratorService
    budget: BudgetMeter
    stop_rules: StopRules
    metrics: Metrics
    executor: Executor
    harness: Harness
    tool_server: ToolServer
    # The compose slot's tool server, over tools built with compose_arm(context.arm)
    # so the compose agent gets the same tool behaviour in every arm (D74).
    compose_tool_server: ToolServer
    target_repository: str
    target_ref: str
    # Set when workers clone their views over the network instead of by path (#129).
    view_publisher: ViewPublisher | None = None
    count_queued_tasks: CountQueuedTasks
    # None: verified claims go straight to the Integrator and nothing in the run
    # scores the verdict split (D66).
    critic: HoldoutCritic | None = None


class Assembly(Protocol):
    def build(self, context: RunContext) -> RunComponents:
        """Build every component of one run, in its own root, namespace and quota."""
        ...
