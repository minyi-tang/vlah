"""A run assembled from fakes and in-memory stores, for the runtime's tests.

The router and the indexer are the real ones over memory stores. Components that do
not exist yet (Integrator, budget, stop rules, metrics) are fakes here.
"""

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

from tests.fixtures.fakes import FakeLeases, FakeRouter, RecordingBudget
from tests.fixtures.herd import TOY_APPROACH, TOY_SOURCE, make_store_blackboard
from tests.fixtures.tools import RecordingRecorder, ScriptedEval
from tests.fixtures.worker import (
    FakeToolServer,
    MemorySessionFiles,
    RecordingTaskQueue,
    TranscriptHarness,
)
from vlah.adapters.memory.executor import MemoryExecutor, ScriptedHarness
from vlah.adapters.memory.model import MemoryModel
from vlah.adapters.memory.store import MemoryKeyValueStore, MemoryVectorIndex
from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import MergedCommit, PublicProblemFiles
from vlah.core.indexer.indexer import StoreIndexer
from vlah.core.metrics.api import StandingMetrics
from vlah.core.problem.api import AdapterChoice, HerdConfig, Problem
from vlah.core.queue.api import Leases
from vlah.core.router.api import Router
from vlah.core.router.router import KeyValueRouter
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import EgressPolicy, Limits, Params
from vlah.core.worker.loop import WorkerDeps
from vlah.runtime.assembly import RunComponents, RunContext
from vlah.runtime.herd import top_level_source
from vlah.tools.herd_tools import HerdTools

BASELINE_ZERO = BaselineRecord(k=0, parent=None, candidates=(), created_ts=0.0)


def toy_problem(root: Path, worker_count: int = 2) -> Problem:
    return Problem(
        root=root,
        status="aligned",
        params=Params(slots_per_approach={TOY_APPROACH: worker_count}),
        gates=(),
        egress=EgressPolicy(),
        sources=(TOY_SOURCE,),
        approaches=(),
        spec_markdown="# Toy\n",
        limits=Limits(eval_machine_hours=10.0, days=1.0),
        target=None,
        public_files=PublicProblemFiles(root=root, files={"spec.md": b"# Toy\n"}),
        herd_config=HerdConfig(
            judges={},
            executor=AdapterChoice(kind="memory"),
            harness=AdapterChoice(kind="scripted"),
        ),
    )


@dataclass(kw_only=True)
class SwitchedRouter(FakeRouter):
    """Rule 2 only: refuting a claim a lease targets interrupts that worker, if the arm allows."""

    arm: ArmConfig
    leases: Leases
    pending: dict[ActorId, Interrupt] = field(default_factory=dict[ActorId, Interrupt])
    overdue_interrupts: list[Interrupt] = field(default_factory=list[Interrupt])
    is_failing: bool = False

    def on_commit(self, merged_commit: MergedCommit) -> tuple[Interrupt, ...]:
        fired: list[Interrupt] = []
        for event in merged_commit.events:
            is_refutation = event.type == "claim_status" and event.to == "refuted"
            for lease in self.leases.active() if is_refutation else ():
                if self.arm.router_rules_2_5 and event.target in lease.target_claims:
                    interrupt = Interrupt(
                        id=f"i-{len(self.pending) + 1}",
                        agent=lease.agent,
                        kind="hard",
                        rule=2,
                        reason=f"{event.target} was refuted",
                        written_ts=0.0,
                    )
                    self.pending[lease.agent] = interrupt
                    fired.append(interrupt)
                else:
                    continue
        return tuple(fired)

    def check(self, agent: ActorId) -> Interrupt | None:
        return self.pending.get(agent)

    def overdue(self, now_seconds: float) -> tuple[Interrupt, ...]:
        if self.is_failing:
            raise ConnectionError("the router's store is down")
        else:
            due, self.overdue_interrupts = tuple(self.overdue_interrupts), []
            return due


@dataclass(kw_only=True)
class FakeIntegrator:
    params: Params
    verified: list[str] = field(default_factory=list[str])
    closeout_count: int = 0
    closeout_fails: bool = False

    def on_verified(self, result_claim_id: str) -> None:
        self.verified.append(result_claim_id)

    def tick(self) -> tuple[Event, ...]:
        return ()

    def current(self) -> BaselineRecord:
        return BASELINE_ZERO

    def closeout(self) -> BaselineRecord:
        self.closeout_count += 1
        if self.closeout_fails:
            raise RuntimeError("the shared merger crashed")
        else:
            return BASELINE_ZERO


@dataclass(kw_only=True)
class ScriptedStopRules:
    stop_at_seconds: float | None = None
    has_fired: bool = False

    def evaluate(self, now_seconds: float) -> Event | None:
        if (
            self.stop_at_seconds is not None
            and now_seconds >= self.stop_at_seconds
            and not self.has_fired
        ):
            self.has_fired = True
            return Event.model_validate(
                {
                    "local_id": "human/00001",
                    "ts": now_seconds,
                    "actor": "human",
                    "role": "human",
                    "type": "stopped",
                    "data": {"reason": "budget"},
                }
            )
        else:
            return None


class ZeroMetrics:
    def standing(self) -> StandingMetrics:
        return StandingMetrics(
            holdout_score=None,
            dead_end_rewalk_count=0,
            insight_reuse_counts={},
            prediction_error_by_agent={},
            prediction_error_by_approach={},
        )


@dataclass(kw_only=True)
class MemoryAssembly:
    """Builds every component from fakes and in-memory stores; remembers what it built.

    The router is the real one over a memory key-value store; use_real_router False
    swaps in SwitchedRouter, whose overdue and failures a test can script.
    """

    clock: FakeClock
    use_real_router: bool = True
    root: Path
    contexts: list[RunContext] = field(default_factory=list[RunContext])
    built: list[RunComponents] = field(default_factory=list[RunComponents])

    def build(self, context: RunContext) -> RunComponents:
        self.contexts.append(context)
        blackboard = make_store_blackboard(self.clock, name=context.run_id)
        blackboard.write(ActorId("human"), {"spec.md": b"# Toy\n"}, "spec")
        leases = FakeLeases(clock=self.clock)
        indexer = StoreIndexer(
            blackboard=blackboard,
            vectors=MemoryVectorIndex(),
            model=MemoryModel(),
            params=context.params,
            clock=self.clock,
            top_level=top_level_source(context.problem.sources),
        )
        router: Router
        if self.use_real_router:
            router = KeyValueRouter(
                store=MemoryKeyValueStore(
                    clock=self.clock, namespace=f"{context.run_id}-router"
                ),
                leases=leases,
                indexer=indexer,
                arm=context.arm,
                params=context.params,
                clock=self.clock,
            )
        else:
            router = SwitchedRouter(arm=context.arm, leases=leases)
        executor = MemoryExecutor(
            clock=self.clock,
            programs={},
            heartbeat_interval_seconds=context.params.heartbeat_interval_seconds,
            sandbox_root=context.run_root / "sandboxes",
        )
        tools = HerdTools(
            blackboard=blackboard,
            objects=None,
            indexer=indexer,
            eval_service=ScriptedEval(),
            leases=leases,
            router=router,
            recorder=RecordingRecorder(),
            arm=context.arm,
            params=context.params,
            clock=self.clock,
            run_id=context.run_id,
        )
        components = RunComponents(
            blackboard=blackboard,
            indexer=indexer,
            task_queue=RecordingTaskQueue(),
            compose_queue=RecordingTaskQueue(),
            leases=leases,
            eval_service=ScriptedEval(),
            router=router,
            integrator=FakeIntegrator(params=context.params),
            budget=RecordingBudget(),
            stop_rules=ScriptedStopRules(),
            metrics=ZeroMetrics(),
            executor=executor,
            sessions=MemorySessionFiles(executor=executor, root=context.run_root),
            harness=TranscriptHarness(
                scripted=ScriptedHarness(default_program="none"), root=context.run_root
            ),
            tool_server=FakeToolServer(),
            registry=tools,
            target_repository="memory://target",
            target_ref="main",
        )
        self.built.append(components)
        return components


@dataclass(kw_only=True)
class WaitingRunner:
    """Stands in for run_worker: records each start and waits for the stop signal."""

    started: list[ActorId] = field(default_factory=list[ActorId])
    crash_first_start: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, agent: ActorId, deps: WorkerDeps) -> None:
        with self.lock:
            is_first = agent not in self.started
            self.started.append(agent)
        if self.crash_first_start and is_first:
            raise RuntimeError(f"{agent} crashed")
        else:
            deps.stop.wait(None)


def random_for_tests() -> Random:
    return Random(0)


def result_ready_event(submitter: ActorId, local_number: int = 1) -> str:
    """A result_ready event file as the eval service writes it."""
    return json.dumps(
        {
            "local_id": f"eval_service/{local_number:05d}",
            "ts": 0.0,
            "actor": "eval_service",
            "role": "eval_service",
            "type": "result_ready",
            "data": {
                "candidate": "9f3a",
                "claim_id": "c-eval_service-00001",
                "approach": "a-01",
                "submitter": submitter,
                "submitter_checkpoint": None,
                "baseline": "baseline@0",
                "paired": {"mean": 0.1, "se": 0.01, "n_items": 5, "n_reps": 1},
                "per_item": "",
                "score": 1.0,
                "level": "toy_slow",
            },
        }
    )
