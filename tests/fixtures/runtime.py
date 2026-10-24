"""A run assembled from fakes and in-memory stores, for the runtime's tests.

The router, the indexer, the budget meter, the stop rules and the metrics are the
real ones over memory stores. The eval client and the Integrator are fakes unless
use_real_eval and use_real_integrator ask for the real ones: the ladder over a fake
slow judge, and the Integrator over the shared merger with an empty baseline@0.
use_git_blackboard puts the blackboard in a git repository under the run root, as
a real run does, so the CLI can read the run's directory afterwards. durable keeps
the whole run on disk: the git blackboard and a SQLite store under the run root,
with the real queue and leases, so building the same run id again is a restarted
process. use_critic adds the Critic between them, wired as the local assembly wires
it, with a fake verdict judge.
"""

import json
import statistics
import threading
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

import yaml

from tests.fixtures.fakes import FakeLeases, FakeRouter
from tests.fixtures.herd import (
    TOY_APPROACH,
    TOY_SEED_COUNT,
    TOY_SOURCE,
    make_store_blackboard,
    passing_gate,
    zero_baseline_scores,
)
from tests.fixtures.tools import RecordingRecorder, ScriptedEval
from tests.fixtures.worker import FakeToolServer, RecordingTaskQueue, TranscriptHarness
from vlah.adapters.memory.executor import MemoryExecutor, ScriptedHarness
from vlah.adapters.memory.model import MemoryModel
from vlah.adapters.memory.signal_source import (
    FAKE_SCORE_FILE_NAME,
    FakeScore,
    FakeSlowSource,
)
from vlah.adapters.memory.store import MemoryKeyValueStore, MemoryVectorIndex
from vlah.adapters.store.sqlite.store import SqliteKeyValueStore
from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard, MergedCommit, PublicProblemFiles
from vlah.core.blackboard.layout import open_blackboard
from vlah.core.budget.meter import KeyValueBudgetMeter
from vlah.core.eval.api import EvalService
from vlah.core.eval.service import LadderEvalService
from vlah.core.indexer.indexer import StoreIndexer
from vlah.core.integrator.api import IntegratorService
from vlah.core.integrator.core_adapter import CoreCalibration
from vlah.core.integrator.service import HerdIntegrator
from vlah.core.metrics.standing import HerdMetrics
from vlah.core.problem.api import AdapterChoice, HerdConfig, Problem
from vlah.core.queue.api import Leases, TaskQueue
from vlah.core.queue.queue import KeyValueLeases, KeyValueTaskQueue
from vlah.core.router.api import Router
from vlah.core.router.router import KeyValueRouter
from vlah.core.stop.rules import BudgetStopRules, EscalationPolicy
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import EgressPolicy, Limits, Params
from vlah.core.types.sources import ScoreResult, SourceDecl
from vlah.core.worker.loop import WorkerDeps
from vlah.ports.signal_source import Dataset
from vlah.ports.store import KeyValueStore
from vlah.roles.critic.holdout import HoldoutCritic, SealedVerdict, integrator_params
from vlah.runtime.assembly import (
    CountQueuedTasks,
    QueuedTaskCounts,
    RunComponents,
    RunContext,
)
from vlah.runtime.compose import compose_arm
from vlah.runtime.herd import top_level_source
from vlah.runtime.local_assembly import DeliverableFiles
from vlah.runtime.status import KeyValueQueueCounts
from vlah.tools.herd_tools import HerdTools

BASELINE_ZERO = BaselineRecord(k=0, parent=None, candidates=(), created_ts=0.0)
# The toy judge's noise, for the Integrator's spot checks; the toy scores are exact.
TOY_SIGMA = 0.5


def toy_candidate(directory: Path, author: ActorId, score: float) -> Path:
    """A candidate the fake judge scores on every seed, as the toy worker writes one."""
    directory.mkdir(parents=True)
    (directory / "policy.py").write_text(f"SCORE = {score}\n", encoding="utf-8")
    (directory / FAKE_SCORE_FILE_NAME).write_text(
        json.dumps({"per_seed": [score] * TOY_SEED_COUNT}), encoding="utf-8"
    )
    manifest = CandidateManifest(
        kind="artifact",
        target="baseline@0",
        approach=TOY_APPROACH,
        author=author,
        created_at=0.0,
    )
    (directory / "manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8"
    )
    return directory


@dataclass(kw_only=True)
class JudgeWaiter:
    """The Integrator's closeout waiter in tests: let the fake judge answer, then tick."""

    clock: FakeClock
    eval_service: EvalService
    sources: tuple[SourceDecl, ...]

    def __call__(self) -> None:
        self.clock.advance(max(source.latency_seconds for source in self.sources) + 1.0)
        self.eval_service.tick()


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
    baseline: BaselineRecord = BASELINE_ZERO

    def on_verified(self, result_claim_id: str) -> None:
        self.verified.append(result_claim_id)

    def tick(self) -> tuple[Event, ...]:
        return ()

    def current(self) -> BaselineRecord:
        return self.baseline

    def closeout(self) -> BaselineRecord:
        self.closeout_count += 1
        if self.closeout_fails:
            raise RuntimeError("the shared merger crashed")
        else:
            return BASELINE_ZERO


@dataclass(frozen=True, kw_only=True)
class FakeQueueCounts:
    task_queue: RecordingTaskQueue
    compose_queue: RecordingTaskQueue

    def __call__(self) -> QueuedTaskCounts:
        return QueuedTaskCounts(
            arm=len(self.task_queue.tasks), compose=len(self.compose_queue.tasks)
        )


@dataclass(frozen=True, kw_only=True)
class VerdictRun:
    directory: Path
    dataset: Dataset
    baseline: str


@dataclass(kw_only=True)
class FakeVerdictJudge:
    """Scores a directory by its fake_score.json, as the fake slow judge does, and
    remembers every run."""

    runs: list[VerdictRun] = field(default_factory=list[VerdictRun])

    def measure_directory(
        self, directory: Path, dataset: Dataset, baseline: str
    ) -> ScoreResult:
        self.runs.append(
            VerdictRun(directory=directory, dataset=dataset, baseline=baseline)
        )
        score_path = directory / FAKE_SCORE_FILE_NAME
        per_seed = (
            FakeScore.model_validate_json(
                score_path.read_text(encoding="utf-8")
            ).per_seed
            if score_path.is_file()
            else (0.0,) * TOY_SEED_COUNT
        )
        return ScoreResult(
            value=statistics.fmean(per_seed), n=len(per_seed), per_seed=per_seed
        )


@dataclass(kw_only=True)
class MemoryAssembly:
    """Builds every component from fakes and in-memory stores; remembers what it built.

    The router, the budget meter, the stop rules and the metrics are the real ones over
    memory key-value stores; use_real_router False
    swaps in SwitchedRouter, whose overdue and failures a test can script.
    """

    clock: FakeClock
    use_real_router: bool = True
    use_real_eval: bool = False
    use_real_integrator: bool = False
    use_git_blackboard: bool = False
    durable: bool = False
    # Needs use_real_eval and use_real_integrator.
    use_critic: bool = False
    verdict_judge: FakeVerdictJudge = field(default_factory=FakeVerdictJudge)
    root: Path
    contexts: list[RunContext] = field(default_factory=list[RunContext])
    built: list[RunComponents] = field(default_factory=list[RunComponents])

    def build(self, context: RunContext) -> RunComponents:
        self.contexts.append(context)
        blackboard: Blackboard = (
            open_blackboard(context.run_root.parent, context.run_id, "blackboard")
            if self.use_git_blackboard or self.durable
            else make_store_blackboard(self.clock, name=context.run_id)
        )
        if not blackboard.list_paths("spec.md", None):
            blackboard.write(ActorId("human"), {"spec.md": b"# Toy\n"}, "spec")
        else:
            pass
        run_store: KeyValueStore
        task_queue: TaskQueue
        compose_queue: TaskQueue
        leases: Leases
        count_queued_tasks: CountQueuedTasks
        if self.durable:
            run_store = SqliteKeyValueStore(
                path=context.run_root / "state.sqlite",
                clock=self.clock,
                namespace=context.run_id,
            )
            durable_queue = KeyValueTaskQueue(store=run_store)
            durable_compose_queue = KeyValueTaskQueue(store=run_store, pool="external")
            task_queue, compose_queue = durable_queue, durable_compose_queue
            leases = KeyValueLeases(
                store=run_store,
                task_queue=task_queue,
                blackboard=blackboard,
                clock=self.clock,
                lease_ttl_seconds=context.params.lease_ttl_seconds,
            )
            count_queued_tasks = KeyValueQueueCounts(
                task_queue=durable_queue, compose_queue=durable_compose_queue
            )
        else:
            run_store = MemoryKeyValueStore(clock=self.clock, namespace=context.run_id)
            recording_queue = RecordingTaskQueue()
            recording_compose_queue = RecordingTaskQueue()
            task_queue, compose_queue = recording_queue, recording_compose_queue
            leases = FakeLeases(clock=self.clock)
            count_queued_tasks = FakeQueueCounts(
                task_queue=recording_queue, compose_queue=recording_compose_queue
            )
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
                store=run_store,
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
        meter = KeyValueBudgetMeter(store=run_store, limits=context.problem.limits)
        top_level = top_level_source(context.problem.sources)
        eval_service: EvalService
        if self.use_real_eval:
            eval_service = LadderEvalService(
                blackboard=blackboard,
                sources=tuple(
                    FakeSlowSource(clock=self.clock, declaration=source)
                    for source in context.problem.sources
                ),
                indexer=indexer,
                budget=meter,
                gates=context.problem.gates,
                gate_runner=passing_gate,
                baseline_scores=zero_baseline_scores,
                arm=context.arm,
                params=context.params,
                clock=self.clock,
                random=context.random,
            )
        else:
            eval_service = ScriptedEval()
        tools, compose_tools = (
            HerdTools(
                blackboard=blackboard,
                objects=None,
                indexer=indexer,
                eval_service=eval_service,
                leases=leases,
                router=router,
                recorder=RecordingRecorder(),
                arm=arm,
                params=context.params,
                clock=self.clock,
                run_id=context.run_id,
                is_compose=is_compose,
            )
            for arm, is_compose in (
                (context.arm, False),
                (compose_arm(context.arm), True),
            )
        )
        integrator: IntegratorService
        if self.use_real_integrator and top_level is not None:
            baseline_directory = context.run_root / "baseline0"
            baseline_directory.mkdir(parents=True, exist_ok=True)
            integrator = HerdIntegrator(
                blackboard=blackboard,
                eval_service=eval_service,
                task_queue=task_queue,
                compose_queue=compose_queue,
                gates=context.problem.gates,
                gate_runner=passing_gate,
                arm=context.arm,
                params=(
                    integrator_params(context.params, context.arm)
                    if self.use_critic
                    else context.params
                ),
                clock=self.clock,
                top_level=top_level,
                work_directory=context.run_root / "integrator",
                baseline_directory=baseline_directory,
                calibration=CoreCalibration(
                    per_seed=(0.0,) * TOY_SEED_COUNT,
                    score=0.0,
                    audit_per_seed=(0.0,) * TOY_SEED_COUNT,
                    sigma_dev=TOY_SIGMA,
                    sigma_audit=TOY_SIGMA,
                ),
                waiter=JudgeWaiter(
                    clock=self.clock,
                    eval_service=eval_service,
                    sources=context.problem.sources,
                ),
            )
        else:
            integrator = FakeIntegrator(params=context.params)
        metrics = HerdMetrics(
            blackboard=blackboard,
            indexer=indexer,
            budget=meter,
            router=router if isinstance(router, KeyValueRouter) else None,
            top_level=top_level_source(context.problem.sources),
        )
        critic: HoldoutCritic | None = None
        if self.use_critic:
            assert isinstance(integrator, HerdIntegrator) and top_level is not None
            critic = HoldoutCritic(
                blackboard=blackboard,
                eval_service=eval_service,
                integrator=integrator,
                arm=context.arm,
                params=context.params,
                clock=self.clock,
                top_level=top_level,
                work_directory=context.run_root / "critic",
                verdict=SealedVerdict(
                    judge=self.verdict_judge,
                    repeat_count=context.params.verdict_repeat_count,
                    deliverable_directory=DeliverableFiles(integrator=integrator),
                    metrics=metrics,
                ),
            )
        else:
            pass
        components = RunComponents(
            blackboard=blackboard,
            indexer=indexer,
            task_queue=task_queue,
            compose_queue=compose_queue,
            leases=leases,
            eval_service=eval_service,
            router=router,
            integrator=integrator,
            budget=meter,
            stop_rules=BudgetStopRules(
                store=run_store,
                blackboard=blackboard,
                meter=meter,
                integrator=integrator,
                limits=context.problem.limits,
                target=context.problem.target,
                policy=EscalationPolicy(),
            ),
            metrics=metrics,
            executor=executor,
            harness=TranscriptHarness(scripted=ScriptedHarness(default_program="none")),
            tool_server=FakeToolServer(tools=tools),
            compose_tool_server=FakeToolServer(tools=compose_tools),
            target_repository="memory://target",
            target_ref="main",
            count_queued_tasks=count_queued_tasks,
            critic=critic,
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
