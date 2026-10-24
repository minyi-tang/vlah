"""vlah demo: a whole herd on a toy problem in one process (quickstart section 2).

Four scripted workers explore three directions for a toy scheduler. An idea is
one file under ideas/ that names what it tries and the gain it brings, and the
toy judge scores a candidate by the ideas it holds, on five seeds, ten minutes
after it is asked. Most ideas help and stack onto the baseline through the real
Integrator, one makes things worse and becomes a dead end, and two workers have
the same idea, so the second copy is a duplicate the eval client skips.

Everything but the workers and the judge is the real runtime: blackboard, queue,
leases, indexer, router, eval client, Integrator, budget, stop rules and the
supervisor with its status file, over in-memory stores. The supervisor's worker
threads only wait; the scripted workers act between ticks on the caller's thread,
on a fake clock, so one seed always gives the same run (runtime.api.RunDemo).
"""

import hashlib
import statistics
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

import yaml

from vlah.adapters.memory.executor import MemoryExecutor, ScriptedHarness
from vlah.adapters.memory.model import MemoryModel
from vlah.adapters.memory.store import (
    MemoryKeyValueStore,
    MemoryVectorIndex,
    MemoryVersionedStore,
)
from vlah.arms.config import ArmName
from vlah.core.blackboard.api import Blackboard, PublicProblemFiles
from vlah.core.blackboard.blackboard import StoreBlackboard
from vlah.core.blackboard.layout import init_layout
from vlah.core.budget.meter import KeyValueBudgetMeter
from vlah.core.eval.api import EvalRequest, EvalService
from vlah.core.eval.service import LadderEvalService
from vlah.core.indexer.indexer import StoreIndexer
from vlah.core.integrator.core_adapter import CoreCalibration
from vlah.core.integrator.service import HerdIntegrator
from vlah.core.metrics.standing import HerdMetrics
from vlah.core.problem.api import AdapterChoice, Approach, HerdConfig, Problem
from vlah.core.queue.queue import KeyValueLeases, KeyValueTaskQueue
from vlah.core.router.router import KeyValueRouter
from vlah.core.stop.rules import BudgetStopRules, EscalationPolicy
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import ResultReady
from vlah.core.types.ids import ActorId, format_claim_id
from vlah.core.types.params import EgressPolicy, Limits, Params
from vlah.core.types.sources import ScoreResult, SourceBudget, SourceDecl
from vlah.core.types.states import ClaimType
from vlah.core.types.tasks import Task
from vlah.core.worker.loop import WorkerDeps
from vlah.ports.signal_source import CandidateRef, Dataset
from vlah.ports.store import VersionedStore
from vlah.runtime.api import Report
from vlah.runtime.assembly import RunComponents, RunContext
from vlah.runtime.compose import compose_arm
from vlah.runtime.herd import HerdRun, build_herd, events_on
from vlah.runtime.local_assembly import CommandGateRunner
from vlah.runtime.status import KeyValueQueueCounts
from vlah.runtime.supervisor import WorkerSlot
from vlah.tools.herd_tools import HerdTools
from vlah.tools.server import McpToolServer

DEMO_PROBLEM_NAME = "toy-scheduler"
DEMO_RUN_ID = "demo"
DEMO_ARM: ArmName = "S2"
DEMO_WORKER_COUNT = 4
STEP_SECONDS = 600.0
DURATION_SECONDS = 8 * 3600.0
SEED_COUNT = 5
BASE_SCORE = 100.0
# How far one idea's gain moves on one seed, so paired gains have a standard error.
NOISE_SCALE = 0.3
# The seed scales each idea's gain by a factor in this range, the same for an
# idea tried in two directions.
GAIN_SCALE_RANGE = (0.7, 1.3)
IDEAS_DIRECTORY = "ideas"
PUSH_TOKEN = "demo-push-token"
TOOL_HOST = "127.0.0.1"
BASELINE_ZERO = "baseline@0"
IDEA_TASK_KINDS = ("advance", "exploration")
SLOTS_PER_APPROACH = {"a-01": 2, "a-02": 1, "a-03": 1}
DIRECTIONS = {
    "a-01": "Batch jobs so the scheduler switches less.",
    "a-02": "Cache what the scheduler looks up again and again.",
    "a-03": "Choose a better order to run the jobs in.",
}
TOY_JUDGE = SourceDecl(
    id="toy_judge",
    mode="pull",
    latency="10m",
    budget=SourceBudget(per_day=24, allocated_by="human"),
    cost_machine_hours=1.0,
    trust=0.9,
    readable_by=("critic",),
)
# The demo spends 11 judge runs; 12 machine-hours puts it past the 80% that
# escalates to the human, and short of the limit that stops the run.
TOY_LIMITS = Limits(eval_machine_hours=12.0, days=1.0)
SPEC = (
    f"# {DEMO_PROBLEM_NAME}\n\n"
    "Make a toy job scheduler finish its jobs sooner. A candidate holds one file "
    "under ideas/ for each idea it applies; the judge scores the ideas together.\n"
)


@dataclass(frozen=True, kw_only=True)
class Idea:
    name: str
    approach: str
    description: str
    gain: float

    @property
    def file_text(self) -> str:
        return f"{self.description}\ngain = {self.gain:.2f}\n"


# Each direction's ideas in the order its workers try them: file name, direction,
# description and the gain before the seed scales it. a-03 repeats a-02's first
# idea once a-02 has had it measured, so the eval client skips the copy.
IDEAS = (
    ("batch_small_jobs", "a-01", "Batch the small jobs together", 2.0),
    ("batch_by_deadline", "a-01", "Batch jobs by deadline window", -0.6),
    ("cache_hot_lookups", "a-02", "Cache the hottest lookups", 3.0),
    ("warm_cache_first", "a-02", "Warm the cache before the first job", 1.5),
    ("shortest_job_first", "a-03", "Run the shortest job first", 2.5),
    ("cache_hot_lookups", "a-03", "Cache the hottest lookups", 3.0),
)


def demo_plan(seed: int) -> dict[str, deque[Idea]]:
    """Each direction's ideas, with the gains this seed gives them."""
    random = Random(seed)
    scales = {
        name: random.uniform(*GAIN_SCALE_RANGE)
        for name in sorted({name for name, _, _, _ in IDEAS})
    }
    return {
        approach: deque(
            Idea(
                name=name,
                approach=approach,
                description=description,
                gain=round(gain * scales[name], 2),
            )
            for name, idea_approach, description, gain in IDEAS
            if idea_approach == approach
        )
        for approach in SLOTS_PER_APPROACH
    }


def seed_jitter(idea_name: str, dataset: Dataset, seed_index: int) -> float:
    """A fixed number in [-1, 1] for one idea on one seed of one dataset."""
    digest = hashlib.sha256(f"{idea_name}:{dataset}:{seed_index}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**31 - 1.0


@dataclass(frozen=True, kw_only=True)
class ToyJob:
    visible_at_seconds: float
    result: ScoreResult


@dataclass(kw_only=True)
class ToyJudge:
    """signal_source.SignalSource: a candidate scores BASE_SCORE plus the gain of
    every idea file it holds, each moved a little on each seed."""

    clock: FakeClock
    jobs: dict[str, ToyJob] = field(default_factory=dict[str, ToyJob])

    @property
    def declaration(self) -> SourceDecl:
        return TOY_JUDGE

    def measure(self, directory: Path, dataset: Dataset) -> ScoreResult:
        idea_files = sorted((directory / IDEAS_DIRECTORY).glob("*.txt"))
        per_seed = tuple(
            BASE_SCORE
            + sum(
                float(path.read_text(encoding="utf-8").rsplit("=", 1)[1])
                + NOISE_SCALE * seed_jitter(path.stem, dataset, seed_index)
                for path in idea_files
            )
            for seed_index in range(SEED_COUNT)
        )
        return ScoreResult(
            value=statistics.fmean(per_seed),
            n=SEED_COUNT,
            per_seed=per_seed,
            constraint_ok=True,
        )

    def submit(
        self, candidate: CandidateRef, actor: ActorId, baseline: str, dataset: Dataset
    ) -> str:
        job_id = f"{TOY_JUDGE.id}-{len(self.jobs) + 1:05d}"
        self.jobs[job_id] = ToyJob(
            visible_at_seconds=self.clock.now_seconds() + TOY_JUDGE.latency_seconds,
            result=self.measure(candidate.payload_directory, dataset),
        )
        return job_id

    def poll(self, job_id: str) -> ScoreResult | None:
        job = self.jobs[job_id]
        if self.clock.now_seconds() >= job.visible_at_seconds:
            return job.result
        else:
            return None


@dataclass(frozen=True, kw_only=True)
class ToyBaselineScores:
    """eval.jobs.BaselineScores: baseline@0 measured, later baselines from their records."""

    judge: ToyJudge
    baseline_directory: Path
    blackboard: Blackboard

    def __call__(
        self, source_id: str, baseline: str, dataset: Dataset
    ) -> tuple[float, ...] | None:
        record_path = f"baseline/{baseline.removeprefix('baseline@')}.json"
        if baseline == BASELINE_ZERO:
            return self.judge.measure(self.baseline_directory, dataset).per_seed
        elif dataset != "dev" or not self.blackboard.list_paths(record_path, None):
            return None
        else:
            record = BaselineRecord.model_validate_json(
                self.blackboard.read(record_path, None)
            )
            return record.per_seed or None


@dataclass(frozen=True, kw_only=True)
class ToyWaiter:
    """The Integrator's closeout waiter: let the toy judge answer, then tick."""

    clock: FakeClock
    eval_service: EvalService

    def __call__(self) -> None:
        self.clock.advance(TOY_JUDGE.latency_seconds + 1.0)
        self.eval_service.tick()


class MemoryMirrorStores:
    def __call__(self, mirror_name: str) -> tuple[VersionedStore, str]:
        return MemoryVersionedStore(name=mirror_name, push_token=PUSH_TOKEN), PUSH_TOKEN


@dataclass(frozen=True, kw_only=True)
class HeldTask:
    """A task a scripted worker took this step; idea is chosen when it is taken."""

    task: Task
    idea: Idea | None


def idle_runner(agent: ActorId, deps: WorkerDeps) -> None:
    """supervisor.WorkerRunner: the demo's workers act on the caller's thread."""
    deps.stop.wait(None)


@dataclass(frozen=True, kw_only=True)
class DemoAssembly:
    """assembly.Assembly: every real component over in-memory stores, and the toy judge."""

    clock: FakeClock

    def build(self, context: RunContext) -> RunComponents:
        clock = self.clock
        params = context.params
        problem = context.problem
        blackboard = StoreBlackboard(
            store=MemoryVersionedStore(name=context.run_id, push_token=PUSH_TOKEN),
            push_token=PUSH_TOKEN,
            objects=None,
            mirror_store_factory=MemoryMirrorStores(),
            clock=clock,
        )
        init_layout(blackboard, problem.public_files)
        store = MemoryKeyValueStore(clock=clock, namespace=context.run_id)
        task_queue = KeyValueTaskQueue(store=store)
        compose_queue = KeyValueTaskQueue(store=store, pool="external")
        leases = KeyValueLeases(
            store=store,
            task_queue=task_queue,
            blackboard=blackboard,
            clock=clock,
            lease_ttl_seconds=params.lease_ttl_seconds,
        )
        indexer = StoreIndexer(
            blackboard=blackboard,
            vectors=MemoryVectorIndex(),
            model=MemoryModel(),
            params=params,
            clock=clock,
            top_level=TOY_JUDGE.id,
        )
        router = KeyValueRouter(
            store=store,
            leases=leases,
            indexer=indexer,
            arm=context.arm,
            params=params,
            clock=clock,
        )
        meter = KeyValueBudgetMeter(store=store, limits=problem.limits)
        judge = ToyJudge(clock=clock)
        baseline_directory = context.run_root / "baseline0"
        baseline_directory.mkdir(parents=True, exist_ok=True)
        gate_runner = CommandGateRunner(problem_root=problem.root)
        eval_service = LadderEvalService(
            blackboard=blackboard,
            sources=(judge,),
            indexer=indexer,
            budget=meter,
            gates=problem.gates,
            gate_runner=gate_runner,
            baseline_scores=ToyBaselineScores(
                judge=judge,
                baseline_directory=baseline_directory,
                blackboard=blackboard,
            ),
            arm=context.arm,
            params=params,
            clock=clock,
            random=context.random,
        )
        daily = judge.measure(baseline_directory, "dev")
        integrator = HerdIntegrator(
            blackboard=blackboard,
            eval_service=eval_service,
            task_queue=task_queue,
            compose_queue=compose_queue,
            gates=problem.gates,
            gate_runner=gate_runner,
            arm=context.arm,
            params=params,
            clock=clock,
            top_level=TOY_JUDGE.id,
            work_directory=context.run_root / "integrator",
            baseline_directory=baseline_directory,
            calibration=CoreCalibration(
                per_seed=daily.per_seed,
                score=BASE_SCORE,
                audit_per_seed=judge.measure(baseline_directory, "audit").per_seed,
                sigma_dev=NOISE_SCALE / SEED_COUNT**0.5,
                sigma_audit=NOISE_SCALE / SEED_COUNT**0.5,
            ),
            waiter=ToyWaiter(clock=clock, eval_service=eval_service),
        )
        tool_server, compose_tool_server = (
            McpToolServer(
                tools=HerdTools(
                    blackboard=blackboard,
                    objects=None,
                    indexer=indexer,
                    eval_service=eval_service,
                    leases=leases,
                    router=router,
                    recorder=None,
                    arm=arm,
                    params=params,
                    clock=clock,
                    run_id=context.run_id,
                    is_compose=is_compose,
                ),
                host=TOOL_HOST,
                port=0,
            )
            for arm, is_compose in (
                (context.arm, False),
                (compose_arm(context.arm), True),
            )
        )
        return RunComponents(
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
                store=store,
                blackboard=blackboard,
                meter=meter,
                integrator=integrator,
                limits=problem.limits,
                target=problem.target,
                policy=EscalationPolicy(),
            ),
            metrics=HerdMetrics(
                blackboard=blackboard,
                indexer=indexer,
                budget=meter,
                router=router,
                top_level=TOY_JUDGE.id,
            ),
            executor=MemoryExecutor(
                clock=clock,
                programs={},
                heartbeat_interval_seconds=params.heartbeat_interval_seconds,
                sandbox_root=context.run_root / "sandboxes",
            ),
            harness=ScriptedHarness(default_program="toy"),
            tool_server=tool_server,
            compose_tool_server=compose_tool_server,
            target_repository="memory://toy-scheduler",
            target_ref=BASELINE_ZERO,
            count_queued_tasks=KeyValueQueueCounts(
                task_queue=task_queue, compose_queue=compose_queue
            ),
        )


@dataclass(kw_only=True)
class ToyWorkers:
    """The scripted workers: each step, a worker finishes the task it took last step
    or takes a new one, through the same queue and leases a real worker gets.

    A task to advance or explore a direction gets that direction's next idea, or
    another direction's once its own are used up; with none left anywhere the
    task goes back to the queue.
    """

    slots: tuple[WorkerSlot, ...]
    blackboard: Blackboard
    eval_service: EvalService
    clock: FakeClock
    plan: dict[str, deque[Idea]]
    candidates_root: Path
    held: dict[ActorId, HeldTask] = field(default_factory=dict[ActorId, HeldTask])
    submitted: dict[str, Idea] = field(default_factory=dict[str, Idea])
    claim_counts: dict[ActorId, int] = field(default_factory=dict[ActorId, int])
    session_count: int = 0

    def step(self) -> None:
        for slot in self.slots:
            held_task = self.held.pop(slot.agent, None)
            new_task = (
                None
                if held_task is not None
                else slot.deps.task_queue.claim_task(slot.agent)
            )
            ideas = (
                next((ideas for ideas in self.plan.values() if ideas), None)
                if new_task is None or not self.plan[new_task.approach]
                else self.plan[new_task.approach]
            )
            if held_task is not None:
                self.finish(slot, held_task)
            elif new_task is None:
                continue
            elif new_task.kind in IDEA_TASK_KINDS and ideas is None:
                slot.deps.task_queue.requeue(new_task.id, "no idea left to try")
            else:
                self.session_count += 1
                slot.deps.leases.acquire(
                    slot.agent, new_task, f"demo-{self.session_count:05d}"
                )
                self.held[slot.agent] = HeldTask(
                    task=new_task,
                    idea=(
                        ideas.popleft()
                        if new_task.kind in IDEA_TASK_KINDS and ideas is not None
                        else None
                    ),
                )

    def finish(self, slot: WorkerSlot, held_task: HeldTask) -> None:
        task = held_task.task
        if held_task.idea is not None:
            self.try_idea(slot.agent, task, held_task.idea)
        elif task.kind == "result_followup":
            self.read_result(slot.agent, task)
        else:
            self.write_claim(
                slot.agent,
                "insight",
                task.approach,
                f"Noted: {task.goal}",
                (
                    EvidenceRef(claim=task.refs[0])
                    if task.refs
                    else EvidenceRef(path="spec.md")
                ),
            )
        slot.deps.leases.release(slot.agent)
        slot.deps.task_queue.complete(task.id, slot.agent)

    def try_idea(self, agent: ActorId, task: Task, idea: Idea) -> None:
        directory = self.candidates_root / agent / task.id
        (directory / IDEAS_DIRECTORY).mkdir(parents=True)
        (directory / IDEAS_DIRECTORY / f"{idea.name}.txt").write_text(
            idea.file_text, encoding="utf-8"
        )
        manifest = CandidateManifest(
            kind="artifact",
            target=BASELINE_ZERO,
            approach=idea.approach,
            author=agent,
            created_at=self.clock.now_seconds(),
        )
        (directory / "manifest.yaml").write_text(
            yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8"
        )
        request = EvalRequest(
            candidate_directory=directory,
            predictions={TOY_JUDGE.id: BASE_SCORE + idea.gain},
            rationale=idea.description,
        )
        response = self.eval_service.request(request, agent)
        if response.status == "needs_confirmation":
            response = self.eval_service.request(
                request.model_copy(update={"confirm_token": response.confirm_token}),
                agent,
            )
        else:
            pass
        if response.claim_id is None:
            raise RuntimeError(
                f"the eval client refused {idea.name}: {response.status} {response.error}"
            )
        else:
            self.submitted[response.claim_id] = idea
        body = (
            f"{idea.description}: sent to the judge."
            if response.status == "queued"
            else f"{idea.description}: already measured as {response.claim_id}."
        )
        self.write_claim(
            agent, "insight", idea.approach, body, EvidenceRef(claim=response.claim_id)
        )

    def read_result(self, agent: ActorId, task: Task) -> None:
        claim_id = task.refs[0]
        result = next(
            event.data
            for event in events_on(self.blackboard)
            if isinstance(event.data, ResultReady) and event.data.claim_id == claim_id
        )
        idea = self.submitted[claim_id]
        gain = (
            result.paired.mean
            if result.paired is not None and result.constraint_ok is not False
            else None
        )
        if gain is None or gain <= 0:
            self.write_claim(
                agent,
                "dead_end",
                task.approach,
                f"{idea.description} does not help: the judge saw no gain.",
                EvidenceRef(claim=claim_id),
            )
        else:
            self.write_claim(
                agent,
                "insight",
                task.approach,
                f"{idea.description} helps: {gain:+.2f} on the judge.",
                EvidenceRef(claim=claim_id),
            )

    def write_claim(
        self,
        agent: ActorId,
        claim_type: ClaimType,
        approach: str,
        body: str,
        evidence: EvidenceRef,
    ) -> None:
        number = self.claim_counts.get(agent, 0) + 1
        self.claim_counts[agent] = number
        claim = Claim.model_validate(
            {
                "id": format_claim_id(agent, number),
                "type": claim_type,
                "status": "proposed",
                "approach": approach,
                "author": agent,
                "evidence": [evidence.model_dump(exclude_none=True)],
                "body": body,
            }
        )
        receipt = self.blackboard.write(
            agent,
            {f"claims/{agent}/{claim.id}.md": render_claim(claim).encode("utf-8")},
            f"{claim_type} from {agent}",
        )
        if receipt.status == "rejected":
            raise RuntimeError(f"the demo's claim was rejected: {receipt.violations}")
        else:
            pass


def toy_problem(root: Path) -> Problem:
    return Problem(
        root=root,
        status="aligned",
        params=Params(slots_per_approach=SLOTS_PER_APPROACH),
        gates=(),
        egress=EgressPolicy(),
        sources=(TOY_JUDGE,),
        approaches=tuple(
            Approach(id=approach, status="active", body=description)
            for approach, description in DIRECTIONS.items()
        ),
        spec_markdown=SPEC,
        limits=TOY_LIMITS,
        target=None,
        public_files=PublicProblemFiles(root=root, files={"spec.md": SPEC.encode()}),
        herd_config=HerdConfig(
            judges={},
            executor=AdapterChoice(kind="memory"),
            harness=AdapterChoice(kind="scripted"),
        ),
    )


def demo_run(seed: int, directory: Path) -> HerdRun:
    """Run the demo to its end under directory and return the stopped run."""
    clock = FakeClock()
    run = build_herd(
        toy_problem(directory),
        DEMO_ARM,
        DemoAssembly(clock=clock),
        DEMO_RUN_ID,
        root=directory / "runs",
        worker_count=DEMO_WORKER_COUNT,
        duration_seconds=DURATION_SECONDS,
        clock=clock,
        random=Random(seed),
        runner=idle_runner,
        poll_seconds=STEP_SECONDS,
    ).prepare()
    workers = ToyWorkers(
        slots=tuple(slot for slot in run.supervisor.slots if not slot.is_compose),
        blackboard=run.components.blackboard,
        eval_service=run.components.eval_service,
        clock=clock,
        plan=demo_plan(seed),
        candidates_root=directory / "candidates",
    )
    while not run.supervisor.is_stopping:
        run.tick(clock.now_seconds())
        workers.step()
        clock.advance(STEP_SECONDS)
    run.stop()
    return run


def run_demo(seed: int) -> Report:
    """runtime.api.RunDemo: the same seed gives the same deliverable."""
    with tempfile.TemporaryDirectory(prefix="vlah-demo-") as directory:
        return demo_run(seed, Path(directory)).report()
