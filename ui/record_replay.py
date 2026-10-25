"""Generate the UI recording by running real VLAH components with scripted I/O."""

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory

import yaml
from pydantic import BaseModel

from tests.fixtures.herd import passing_gate
from tests.fixtures.integrator import ScriptedMeasurer, per_seed
from tests.fixtures.runtime import MemoryAssembly, toy_problem
from ui.replay_recording import TraceRecorder
from vlah.adapters.memory.executor import (
    MemoryExecutor,
    ScriptedHarness,
    ScriptedSession,
)
from vlah.adapters.memory.signal_source import FakeSlowSource
from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.arms.config import PRESETS
from vlah.core.eval.api import EvalRequest
from vlah.core.eval.service import LadderEvalService
from vlah.core.integrator.core_adapter import CoreCalibration
from vlah.core.integrator.service import HerdIntegrator
from vlah.core.queue.queue import KeyValueLeases, KeyValueTaskQueue
from vlah.core.router.router import KeyValueRouter
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.claims import Claim, render_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId, format_claim_id
from vlah.core.types.sources import ScoreResult, SourceDecl
from vlah.ports.harness import Workspace
from vlah.ports.signal_source import CandidateRef, Dataset
from vlah.runtime.api import Report, RunStatus
from vlah.runtime.assembly import RunComponents, RunContext
from vlah.runtime.compose import COMPOSE_AGENT
from vlah.runtime.herd import RunFanout, baselines_on
from vlah.runtime.wiring import HerdComponents, SteppedHerd

WORKER_COUNT = 100
DURATION_SECONDS = 160


class ComposeContext(BaseModel):
    ours_dir: str
    theirs_dir: str
    target: str


class FeatureJudge(FakeSlowSource):
    def __init__(
        self, clock: FakeClock, declaration: SourceDecl, measurer: ScriptedMeasurer
    ) -> None:
        super().__init__(clock=clock, declaration=declaration)
        self.measurer = measurer

    def score_for(self, candidate: CandidateRef, dataset: Dataset) -> ScoreResult:
        scores = tuple(
            per_seed(self.measurer.gain_of(candidate.payload_directory, dataset))
        )
        return ScoreResult(
            value=sum(scores) / len(scores),
            n=len(scores),
            per_seed=scores,
            constraint_ok=True,
        )


@dataclasses.dataclass
class RecordedWork:
    components: RunComponents
    integrator: HerdIntegrator
    recorder: TraceRecorder
    measurer: ScriptedMeasurer
    root: Path
    first_candidates: dict[str, str] = dataclasses.field(default_factory=dict)
    claims: dict[str, int] = dataclasses.field(default_factory=dict)
    attempts: dict[str, int] = dataclasses.field(default_factory=dict)

    def claim(
        self,
        session: ScriptedSession,
        kind: str,
        body: str,
        references: tuple[str, ...] = (),
    ) -> None:
        number = self.claims.get(session.agent, 0) + 1
        self.claims[session.agent] = number
        claim = Claim.model_validate(
            {
                "id": format_claim_id(session.agent, number),
                "type": kind,
                "status": "open" if kind == "question" else "proposed",
                "approach": session.task.approach,
                "author": session.agent,
                "depends_on": references,
                "evidence": [{"claim": reference} for reference in references]
                or [{"path": "spec.md"}],
                "body": body,
            }
        )
        receipt = self.components.blackboard.write(
            session.agent,
            {f"claims/{session.agent}/{claim.id}.md": render_claim(claim).encode()},
            "scripted worker contribution",
        )
        if receipt.status == "rejected":
            raise RuntimeError(str(receipt.violations))
        else:
            pass

    def candidate(
        self,
        session: ScriptedSession,
        parents: tuple[str, ...],
        gain: float,
        compose: ComposeContext | None = None,
    ) -> None:
        attempt = self.attempts.get(session.agent, 0) + 1
        self.attempts[session.agent] = attempt
        current = self.integrator.current()
        target = compose.target if compose else f"baseline@{current.k}"
        version = self.integrator.core.versions[int(target.split("@")[1])]
        directory = self.root / "submissions" / session.agent / str(attempt)
        directory.mkdir(parents=True)
        base = Path(version.dir)
        for path in base.rglob("*"):
            if path.is_file() and path.name != "manifest.yaml":
                destination = directory / path.relative_to(base)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(path.read_bytes())
            else:
                pass
        marker = f"feature-{session.agent}-{attempt}\n"
        self.measurer.dev_gain_by_marker[marker] = gain
        self.measurer.audit_gain_by_marker[marker] = gain
        if compose:
            combined = []
            for source in (Path(compose.ours_dir), Path(compose.theirs_dir)):
                for path in source.rglob("*"):
                    if path.is_file() and path.name != "manifest.yaml":
                        combined.append(path.read_text())
                    else:
                        pass
            (directory / "composition.txt").write_text("\n".join(combined) + marker)
        elif session.agent in ("worker-07", "worker-08") and attempt == 1:
            (directory / "shared-policy.txt").write_text(marker)
        else:
            (directory / f"{session.agent}-{attempt}.txt").write_text(marker)
        manifest = CandidateManifest(
            kind="artifact",
            target=target,
            parents=parents,
            approach=session.task.approach,
            author=session.agent,
            created_at=session.clock.now_seconds(),
            note="Scripted candidate exercising actual evaluation and integration.",
        )
        (directory / "manifest.yaml").write_text(
            yaml.safe_dump(manifest.model_dump(mode="json"))
        )
        request = EvalRequest(
            candidate_directory=directory,
            parents=parents,
            predictions={self.integrator.top_level: 10 + gain},
            rationale="Scripted local reproduction; scores come from the in-memory judge.",
            baseline=target,
            kind="compose" if compose else "worker",
        )
        response = self.components.eval_service.request(request, session.agent)
        if response.status == "needs_confirmation":
            response = self.components.eval_service.request(
                request.model_copy(update={"confirm_token": response.confirm_token}),
                session.agent,
            )
        else:
            pass
        if response.status != "queued" or response.claim_id is None:
            raise RuntimeError(
                f"Candidate rejected: {response.status}: {response.error}"
            )
        else:
            self.claim(
                session,
                "insight",
                "Submitted a candidate; its result is still pending.",
                (response.claim_id,),
            )

    def __call__(self, session: ScriptedSession) -> Iterator[float]:
        self.recorder.begin(
            session.agent, session.task, f"{session.agent}/{session.task.id}"
        )
        try:
            number = (
                int(session.agent.rsplit("-", 1)[1])
                if session.agent != COMPOSE_AGENT
                else 0
            )
            if session.task.kind == "advance":
                yield 4 + number % 12
                if number <= 8:
                    self.candidate(
                        session, (), -1.0 if number == 4 else 0.5 + number / 3
                    )
                else:
                    self.claim(
                        session,
                        "question",
                        "Inspecting another part of the shared problem; no evaluated candidate is claimed.",
                    )
            elif session.task.kind == "result_followup":
                yield 6
                candidate = session.task.refs[1]
                self.first_candidates.setdefault(session.agent, candidate)
                if number in (1, 2, 3) and self.attempts.get(session.agent, 0) == 1:
                    other = self.first_candidates.get(
                        "worker-02" if number == 1 else "worker-01"
                    )
                    parents = (
                        (candidate, other)
                        if other is not None and other != candidate
                        else (candidate,)
                    )
                    self.claim(
                        session,
                        "insight",
                        "The returned result suggests a follow-up that reuses an earlier candidate.",
                        (session.task.refs[0],),
                    )
                    self.candidate(session, parents, 1.2)
                else:
                    self.claim(
                        session,
                        "insight",
                        "The returned result has been reviewed; this task is complete.",
                        (session.task.refs[0],),
                    )
            elif session.task.kind == "integration_failed_followup":
                yield 6
                self.claim(
                    session,
                    "dead_end",
                    "Integration did not preserve a useful gain; retain this failed route and its evidence.",
                    (session.task.refs[0],),
                )
            elif session.task.kind == "rebase_compose":
                yield 9
                if session.task.seed_context is None:
                    raise RuntimeError("Compose task has no context.")
                else:
                    self.candidate(
                        session,
                        session.task.refs,
                        0.8,
                        ComposeContext.model_validate_json(session.task.seed_context),
                    )
            else:
                raise RuntimeError(f"Unscripted task kind {session.task.kind}")
        finally:
            self.recorder.finish(session.agent)


def record(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    clock = FakeClock()
    problem = toy_problem(root, worker_count=WORKER_COUNT)
    declaration = SourceDecl.model_validate(
        {**problem.sources[0].model_dump(), "latency": "12s"}
    )
    problem = dataclasses.replace(problem, sources=(declaration,))
    context = RunContext(
        run_id="recorded-core",
        run_root=root,
        arm=PRESETS["S2"],
        params=problem.params,
        problem=problem,
        clock=clock,
        random=Random(0),
        duration_seconds=DURATION_SECONDS,
    )
    components = MemoryAssembly(clock=clock, root=root).build(context)
    store = MemoryKeyValueStore(clock=clock, namespace="recorded-core")
    task_queue = KeyValueTaskQueue(store=store)
    compose_queue = KeyValueTaskQueue(store=store, pool="external")
    leases = KeyValueLeases(
        store=store,
        task_queue=task_queue,
        blackboard=components.blackboard,
        clock=clock,
        lease_ttl_seconds=problem.params.lease_ttl_seconds,
    )
    router = KeyValueRouter(
        store=store,
        leases=leases,
        indexer=components.indexer,
        arm=context.arm,
        params=context.params,
        clock=clock,
    )
    measurer = ScriptedMeasurer()
    source = FeatureJudge(clock, declaration, measurer)
    baseline = root / "initial"
    baseline.mkdir()
    (baseline / "specimen.txt").write_text("baseline\n")
    integrator = HerdIntegrator(
        blackboard=components.blackboard,
        eval_service=components.eval_service,
        task_queue=task_queue,
        compose_queue=compose_queue,
        gates=(),
        gate_runner=passing_gate,
        arm=context.arm,
        params=context.params,
        clock=clock,
        top_level=declaration.id,
        work_directory=root / "integrator",
        baseline_directory=baseline,
        calibration=CoreCalibration(
            per_seed=tuple(per_seed(0)),
            score=10.0,
            audit_per_seed=tuple(per_seed(0)),
            sigma_dev=0.5,
            sigma_audit=0.5,
        ),
        measurer=measurer,
    )

    def baseline_scores(
        source_id: str, baseline: str, dataset: Dataset
    ) -> tuple[float, ...]:
        version = integrator.core.versions[int(baseline.split("@")[1])]
        return tuple(version.per_seed)

    evaluator = LadderEvalService(
        blackboard=components.blackboard,
        sources=(source,),
        indexer=components.indexer,
        budget=components.budget,
        gates=(),
        gate_runner=passing_gate,
        baseline_scores=baseline_scores,
        arm=context.arm,
        params=context.params,
        clock=clock,
        random=context.random,
    )
    executor = MemoryExecutor(
        clock=clock,
        programs={},
        heartbeat_interval_seconds=context.params.heartbeat_interval_seconds,
        sandbox_root=root / "sandboxes",
    )
    harness = ScriptedHarness(default_program="recorded")
    components = dataclasses.replace(
        components,
        task_queue=task_queue,
        compose_queue=compose_queue,
        leases=leases,
        router=router,
        eval_service=evaluator,
        integrator=integrator,
        executor=executor,
        harness=harness,
    )
    recorder = TraceRecorder(
        components.blackboard,
        components.indexer,
        clock,
        provenance="Recorded from VLAH RunFanout, KeyValueTaskQueue, StoreIndexer, LadderEvalService and HerdIntegrator. Clock, workers and judge are scripted in-memory adapters; this is not a production run.",
    )
    fanout = RunFanout(components=components, top_level=declaration.id)
    components.blackboard.subscribe(fanout)
    components.blackboard.subscribe(recorder)
    recorder.add_node(
        "baseline@0",
        "baseline",
        "baseline@0",
        "Initial baseline; n=5.",
        "integrator",
        "shared",
        "baseline/0.json",
        "accepted",
    )
    work = RecordedWork(components, integrator, recorder, measurer, root)
    executor.programs["recorded"] = work
    worker_ids = tuple(
        ActorId(f"worker-{index:02d}") for index in range(1, WORKER_COUNT + 1)
    )
    shared = HerdComponents(
        blackboard=components.blackboard,
        indexer=components.indexer,
        task_queue=task_queue,
        leases=leases,
        eval_service=evaluator,
        router=router,
    )
    workspace = Workspace(
        target_repository="memory://recorded",
        target_ref="baseline@0",
        blackboard_clone_url=components.blackboard.clone_url(),
    )
    herd = SteppedHerd(
        arm=context.arm,
        params=context.params,
        clock=clock,
        executor=executor,
        harness=harness,
        components=shared,
        worker_ids=worker_ids,
        workspace=workspace,
    )
    compose = SteppedHerd(
        arm=context.arm,
        params=context.params,
        clock=clock,
        executor=executor,
        harness=harness,
        components=dataclasses.replace(shared, task_queue=compose_queue),
        worker_ids=(COMPOSE_AGENT,),
        workspace=workspace,
    )
    task_queue.maintain(context.params.slots_per_approach)
    for _ in range(DURATION_SECONDS):
        compose.start_idle_workers()
        herd.step(1)
        compose.finish_exited_workers()
        integrator.tick()
        if (
            not herd.running
            and not compose.running
            and not task_queue.queued_tasks()
            and not compose_queue.queued_tasks()
            and all(
                job.visible_at_seconds <= clock.now_seconds()
                for job in source.jobs.values()
            )
            and not integrator.pending_claims
            and integrator.core.due(clock.now_seconds()) not in ("now", "wait")
        ):
            break
        else:
            pass
    if (
        herd.running
        or compose.running
        or any(
            process.exit_info is None or process.exit_info.return_code != 0
            for process in executor.processes.values()
        )
    ):
        raise RuntimeError("Recording ended with unfinished scripted sessions.")
    else:
        deliverable = integrator.closeout()
        recorder.record_status(
            RunStatus(
                run_id=context.run_id,
                arm=context.arm.arm,
                is_running=False,
                head_sequence=components.blackboard.head_sequence(),
                current_baseline=deliverable.k,
                judge_runs_used=fanout.judge_runs,
                worker_count=WORKER_COUNT,
            )
        )
        recorder.record_report(
            Report(
                run_id=context.run_id,
                arm=context.arm.arm,
                deliverable=deliverable,
                baselines=baselines_on(components.blackboard),
                metrics=components.metrics.standing(),
            )
        )
        last_observation = max(
            [event.at for event in recorder.events]
            + [session.end or session.start for session in recorder.sessions]
            + [clock.now_seconds()]
        )
        return recorder.export(
            (*worker_ids, COMPOSE_AGENT), last_observation + 3, COMPOSE_AGENT
        )


if __name__ == "__main__":
    with TemporaryDirectory(prefix="vlah-ui-recording-") as temporary:
        payload = record(Path(temporary))
    destination = Path(__file__).parent / "src" / "recording.ts"
    destination.write_text(
        f"// Generated by uv run python -m ui.record_replay; do not edit.\nexport const RECORDING = {payload} as const;\n"
    )
    public = Path(__file__).parent / "public" / "recording.json"
    public.write_text(payload + "\n")
    print(f"Recorded VLAH execution in {destination}")
