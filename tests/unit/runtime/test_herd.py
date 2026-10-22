"""The composition root and its merge fan-out: where results and verified claims go (D58, D74)."""

import dataclasses
import json
from pathlib import Path
from random import Random

import pytest
import yaml

from tests.fixtures.fakes import RecordingBudget
from tests.fixtures.herd import (
    TOY_APPROACH,
    TOY_SOURCE,
    passing_gate,
    zero_baseline_scores,
)
from tests.fixtures.runtime import (
    FakeIntegrator,
    MemoryAssembly,
    WaitingRunner,
    random_for_tests,
    result_ready_event,
    toy_problem,
)
from vlah.adapters.memory.signal_source import FAKE_SCORE_FILE_NAME, FakeSlowSource
from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.arms.config import PRESETS, ArmName
from vlah.core.eval.api import EvalRequest, RemeasureRequest
from vlah.core.eval.service import LadderEvalService
from vlah.core.queue.queue import KeyValueTaskQueue
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import RemeasureReady, ResultReady
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import SourceDecl
from vlah.runtime.assembly import RunComponents, RunContext
from vlah.runtime.compose import COMPOSE_AGENT
from vlah.runtime.herd import RunFanout, build_herd, events_on, top_level_source
from vlah.tools.herd_tools import HerdTools

pytestmark = pytest.mark.runtime

WORKER = ActorId("worker-01")
JUDGE_SECONDS = 700.0


def built_components(
    tmp_path: Path, clock: FakeClock, sources: tuple[SourceDecl, ...] = (TOY_SOURCE,)
) -> tuple[RunContext, RunComponents]:
    """One run's components without a herd around them, so a test subscribes its own fan-out."""
    problem = dataclasses.replace(toy_problem(tmp_path), sources=sources)
    context = RunContext(
        run_id="r-1",
        run_root=tmp_path / "r-1",
        arm=PRESETS["S2"],
        params=problem.params,
        problem=problem,
        clock=clock,
        random=random_for_tests(),
        duration_seconds=60.0,
    )
    return context, MemoryAssembly(clock=clock, root=tmp_path).build(context)


def with_real_eval(
    context: RunContext, components: RunComponents
) -> tuple[RunComponents, LadderEvalService]:
    eval_service = LadderEvalService(
        blackboard=components.blackboard,
        sources=tuple(
            FakeSlowSource(clock=context.clock, declaration=source)
            for source in context.problem.sources
        ),
        indexer=components.indexer,
        budget=RecordingBudget(),
        gates=(),
        gate_runner=passing_gate,
        baseline_scores=zero_baseline_scores,
        arm=context.arm,
        params=context.params,
        clock=context.clock,
        random=Random(0),
    )
    return dataclasses.replace(components, eval_service=eval_service), eval_service


def write_candidate(directory: Path, author: ActorId, score: float) -> Path:
    directory.mkdir(parents=True)
    (directory / "policy.py").write_text(f"SCORE = {score}\n", encoding="utf-8")
    (directory / FAKE_SCORE_FILE_NAME).write_text(
        json.dumps({"per_seed": [score] * 5}), encoding="utf-8"
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


def measure_everything(
    clock: FakeClock, eval_service: LadderEvalService, levels: int
) -> None:
    """Each level takes one tick to submit and one, after the latency, to read."""
    for _ in range(2 * levels):
        clock.advance(JUDGE_SECONDS)
        eval_service.tick()


@pytest.mark.parametrize("submitter", [COMPOSE_AGENT, WORKER])
def test_a_results_follow_up_goes_to_the_submitters_queue(
    tmp_path: Path, submitter: ActorId
) -> None:
    """A compose result's follow-up landed in the arm queue, where any arm worker
    could claim it once the compose slot held no lease (#56 R2, D74)."""
    clock = FakeClock()
    context, components = built_components(tmp_path, clock)
    store = MemoryKeyValueStore(clock=clock, namespace="queues")
    components = dataclasses.replace(
        components,
        task_queue=KeyValueTaskQueue(store=store),
        compose_queue=KeyValueTaskQueue(store=store, pool="external"),
    )
    components.blackboard.subscribe(
        RunFanout(components=components, top_level=TOY_SOURCE.id)
    )
    components.blackboard.write(
        ActorId("eval_service"),
        {"events/eval_service/00001.json": result_ready_event(submitter).encode()},
        "result",
    )
    is_compose = submitter == COMPOSE_AGENT
    compose_task = components.compose_queue.claim_task(COMPOSE_AGENT)
    arm_task = components.task_queue.claim_task(WORKER)
    assert (compose_task is not None) is is_compose
    assert (arm_task is not None) is not is_compose
    assert compose_task is None or compose_task.pool == "external"
    other_arm_worker = components.task_queue.claim_task(ActorId("worker-02"))
    assert other_arm_worker is None, "a follow-up is for its submitter"


def test_the_integrators_remeasure_is_not_fed_back_as_verified(
    tmp_path: Path,
) -> None:
    """A remeasure's claim reaches verified at the top level like a worker's, so the
    fan-out handed the Integrator its own remeasure as a new candidate (#56 R3)."""
    clock = FakeClock()
    context, components = built_components(tmp_path, clock)
    components, eval_service = with_real_eval(context, components)
    blackboard = components.blackboard
    blackboard.subscribe(
        RunFanout(components=components, top_level=top_level_source((TOY_SOURCE,)))
    )
    response = eval_service.request(
        EvalRequest(
            candidate_directory=write_candidate(tmp_path / "worker", WORKER, 2.0),
            predictions={TOY_SOURCE.id: 2.0},
            rationale="the worker's reasoning stays with its claim",
        ),
        WORKER,
    )
    assert response.status == "queued" and response.claim_id is not None
    eval_service.remeasure(
        RemeasureRequest(
            candidate_directory=write_candidate(
                tmp_path / "rebased", ActorId("integrator"), 3.0
            ),
            baseline="baseline@0",
            parent_candidate="0" * 12,
            approach=TOY_APPROACH,
        )
    )
    measure_everything(clock, eval_service, levels=1)
    remeasure_claims = [
        event.data.claim_id
        for event in events_on(blackboard)
        if isinstance(event.data, RemeasureReady)
    ]
    verified_at_top = [
        event.target
        for event in events_on(blackboard)
        if event.type == "claim_status" and event.to == f"verified:{TOY_SOURCE.id}"
    ]
    integrator = components.integrator
    assert isinstance(integrator, FakeIntegrator)
    assert len(remeasure_claims) == 1 and remeasure_claims[0] in verified_at_top
    assert integrator.verified == [response.claim_id]


def test_the_top_level_agrees_with_the_eval_service_on_ties(tmp_path: Path) -> None:
    """With two judges of equal cost and latency the runtime took the first and the
    ladder the last, so on_verified never fired (#56 R6)."""
    judges = (
        TOY_SOURCE.model_copy(update={"id": "judge_a"}),
        TOY_SOURCE.model_copy(update={"id": "judge_b"}),
    )
    clock = FakeClock()
    context, components = built_components(tmp_path, clock, judges)
    components, eval_service = with_real_eval(context, components)
    components.blackboard.subscribe(
        RunFanout(components=components, top_level=top_level_source(judges))
    )
    response = eval_service.request(
        EvalRequest(
            candidate_directory=write_candidate(tmp_path / "worker", WORKER, 2.0),
            predictions={judge.id: 2.0 for judge in judges},
            rationale="two judges, one top level",
        ),
        WORKER,
    )
    measure_everything(clock, eval_service, levels=2)
    (result,) = [
        event.data
        for event in events_on(components.blackboard)
        if isinstance(event.data, ResultReady)
    ]
    assert result.level == top_level_source(judges), "the ladder's last level"
    integrator = components.integrator
    assert isinstance(integrator, FakeIntegrator)
    assert integrator.verified == [response.claim_id]


@pytest.mark.parametrize("arm", ["B1", "S1", "S2"])
def test_the_compose_slot_gets_the_same_tools_in_every_arm(
    tmp_path: Path, arm: ArmName
) -> None:
    """The compose agent used the arm's tool server, so its eval_request carried the
    sync-point diff in S1 and S2 and not in B1 (#56 R5, D74)."""
    clock = FakeClock()
    run = build_herd(
        toy_problem(tmp_path),
        arm,
        MemoryAssembly(clock=clock, root=tmp_path),
        f"r-{arm}",
        root=tmp_path,
        worker_count=1,
        duration_seconds=60.0,
        clock=clock,
        random=random_for_tests(),
        runner=WaitingRunner(),
    ).prepare()
    components = run.components
    (compose,) = [slot for slot in run.supervisor.slots if slot.is_compose]
    assert compose.deps.registry is components.compose_registry
    assert compose.deps.tool_server is components.compose_tool_server
    assert compose.deps.registry is not components.registry
    tools = compose.deps.registry
    assert isinstance(tools, HerdTools)
    assert tools.arm.arm == arm, "the recorder still attributes the run to its arm"
    assert tools.arm.diff_since == "off" and not tools.arm.failure_refresh
    tools.register_agent(COMPOSE_AGENT, tmp_path)
    (tmp_path / "composed").mkdir()
    reply = tools.eval_request(
        COMPOSE_AGENT, "composed", {TOY_SOURCE.id: 1.0}, "a and b", (), None, None
    )
    assert reply.diff is None, "no sync-point diff for compose, whatever the arm"
    run.supervisor.finish()


def test_b0_is_one_agent(tmp_path: Path) -> None:
    """D58: B0 is a single agent; a second worker would make it a sharing arm."""
    clock = FakeClock()
    for worker_count in (1, 2):
        herd = build_herd
        if worker_count == 1:
            run = herd(
                toy_problem(tmp_path, worker_count),
                "B0",
                MemoryAssembly(clock=clock, root=tmp_path),
                "r-B0",
                root=tmp_path,
                worker_count=worker_count,
                duration_seconds=60.0,
                clock=clock,
                random=random_for_tests(),
                runner=WaitingRunner(),
            ).prepare()
            pool = [slot for slot in run.supervisor.slots if not slot.is_compose]
            assert len(pool) == 1
            run.supervisor.finish()
        else:
            with pytest.raises(ValueError, match="one agent"):
                herd(
                    toy_problem(tmp_path, worker_count),
                    "B0",
                    MemoryAssembly(clock=clock, root=tmp_path),
                    "r-B0",
                    root=tmp_path,
                    worker_count=worker_count,
                    duration_seconds=60.0,
                    clock=clock,
                    random=random_for_tests(),
                    runner=WaitingRunner(),
                )
