"""A run resumed in a new process continues where the earlier one stopped (D13, D32, D56, #120).

Each half builds the run from the same assembly, which keeps it on disk: the git
blackboard and the SQLite store under the run root. The test plays the workers
itself, so every task claimed is known.
"""

import dataclasses
import json
from pathlib import Path

import pytest
import yaml

from tests.fixtures.herd import TOY_APPROACH, TOY_SEED_COUNT, TOY_SOURCE
from tests.fixtures.runtime import (
    MemoryAssembly,
    WaitingRunner,
    random_for_tests,
    toy_problem,
)
from vlah.adapters.memory.signal_source import FAKE_SCORE_FILE_NAME
from vlah.core.blackboard.api import Blackboard
from vlah.core.eval.api import EvalRequest
from vlah.core.queue.queue import KeyValueTaskQueue
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import RemeasureReady
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.runtime.herd import HerdRun, build_herd, events_on
from vlah.runtime.status import RunStatusFile

pytestmark = pytest.mark.runtime

DURATION_SECONDS = 36_000.0
WORKER_ONE = ActorId("worker-01")
WORKER_TWO = ActorId("worker-02")
RUN_ID = "r-resume"


def prepare(
    assembly: MemoryAssembly,
    clock: FakeClock,
    tmp_path: Path,
    params: Params | None = None,
) -> HerdRun:
    """One process of the run; every half starts from the run's first start."""
    problem = toy_problem(tmp_path)
    return build_herd(
        dataclasses.replace(problem, params=params or problem.params),
        "S2",
        assembly,
        RUN_ID,
        root=tmp_path / "runs",
        worker_count=2,
        duration_seconds=DURATION_SECONDS,
        clock=clock,
        random=random_for_tests(),
        runner=WaitingRunner(),
        started_seconds=0.0,
    ).prepare()


def write_candidate(
    directory: Path,
    author: ActorId,
    scores: dict[str, float],
    baseline: int,
    audit_score: float | None = None,
) -> Path:
    """A full payload: one line per contribution, scored by their sum on every seed,
    and on the audit set by audit_score when given."""
    directory.mkdir(parents=True)
    (directory / "policy.py").write_text(
        "".join(f"{name} = {score}\n" for name, score in sorted(scores.items())),
        encoding="utf-8",
    )
    total = sum(scores.values())
    audit = total if audit_score is None else audit_score
    (directory / FAKE_SCORE_FILE_NAME).write_text(
        json.dumps(
            {
                "per_seed": [total] * TOY_SEED_COUNT,
                "audit_per_seed": [audit] * TOY_SEED_COUNT,
            }
        ),
        encoding="utf-8",
    )
    manifest = CandidateManifest(
        kind="artifact",
        target=f"baseline@{baseline}",
        approach=TOY_APPROACH,
        author=author,
        created_at=0.0,
    )
    (directory / "manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8"
    )
    return directory


def submit_on_a_task(run: HerdRun, agent: ActorId, candidate_directory: Path) -> str:
    """One worker's turn: claim a task, submit under its lease, and finish it."""
    components = run.components
    task = components.task_queue.claim_task(agent)
    assert task is not None
    components.leases.acquire(agent, task, f"handle-{task.id}")
    response = components.eval_service.request(
        EvalRequest(
            candidate_directory=candidate_directory,
            predictions={TOY_SOURCE.id: 1.0},
            rationale=f"{agent} on {task.id}",
        ),
        agent,
    )
    assert response.status == "queued", response.error
    components.leases.release(agent)
    components.task_queue.complete(task.id, agent)
    return task.id


def judge_and_integrate(run: HerdRun, clock: FakeClock, ticks: int = 3) -> None:
    for _ in range(ticks):
        clock.advance(TOY_SOURCE.latency_seconds + 1.0)
        run.supervisor.tick(clock.now_seconds())


def assert_each_event_once(blackboard: Blackboard) -> None:
    """Every event file has exactly one sequence number, and no decision repeats."""
    head = blackboard.head_sequence()
    numbered = [
        blackboard.read(f"events/_seq/{sequence}", None).decode()
        for sequence in range(1, head + 1)
    ]
    written = [
        path
        for path in blackboard.list_paths("events/", None)
        if not path.startswith("events/_seq/")
    ]
    assert sorted(numbered) == sorted(written), "no gaps, no number given twice"
    decisions = [
        (event.type, event.target, event.data.model_dump_json() if event.data else "")
        for event in events_on(blackboard)
        if event.actor == "integrator" or event.type == "result_ready"
    ]
    assert len(decisions) == len(set(decisions)), "no decision written twice"


def queued_task_ids(run: HerdRun) -> list[str]:
    queue = run.components.task_queue
    assert isinstance(queue, KeyValueTaskQueue)
    return [task.id for task, _ in queue.queued_tasks()]


def test_a_stopped_run_resumes_on_its_baseline_without_repeating_work(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    assembly = MemoryAssembly(
        clock=clock,
        root=tmp_path,
        use_real_eval=True,
        use_real_integrator=True,
        durable=True,
    )
    first = prepare(assembly, clock, tmp_path)
    first.supervisor.tick(clock.now_seconds())
    first_task = submit_on_a_task(
        first, WORKER_ONE, write_candidate(tmp_path / "a", WORKER_ONE, {"A": 2.0}, 0)
    )
    judge_and_integrate(first, clock)
    first.stop()
    assert first.report().deliverable.k == 1
    follow_ups_before = [
        task_id for task_id in queued_task_ids(first) if task_id != first_task
    ]

    second = prepare(assembly, clock, tmp_path)
    second.write_status()
    status = RunStatusFile.model_validate_json(second.status_path.read_bytes())
    assert (status.started_ts, status.deadline_ts) == (0.0, DURATION_SECONDS)
    assert status.judge.runs_used == first.fanout.judge_runs > 0, "counted again"
    assert second.components.integrator.current().k == 1
    assert queued_task_ids(second) == follow_ups_before, "nothing queued twice"
    second.supervisor.tick(clock.now_seconds())
    second_task = submit_on_a_task(
        second,
        WORKER_TWO,
        write_candidate(tmp_path / "b", WORKER_TWO, {"A": 2.0, "B": 1.0}, 1),
    )
    judge_and_integrate(second, clock)
    second.stop()

    deliverable = second.report().deliverable
    assert (deliverable.k, deliverable.parent, len(deliverable.candidates)) == (2, 1, 2)
    assert first_task != second_task
    assert first_task not in queued_task_ids(second), "a finished task never returns"
    audited = [
        event.data.baseline
        for event in events_on(second.components.blackboard)
        if isinstance(event.data, RemeasureReady) and event.data.dataset == "audit"
    ]
    assert audited == ["baseline@1", "baseline@2"], "each version audited once"
    assert_each_event_once(second.components.blackboard)


def test_a_crashed_run_resumes_its_leases_and_verified_claims(tmp_path: Path) -> None:
    """The crash drops the process without a closeout: a worker still holds a lease,
    and a verified candidate waits in the Integrator's queue."""
    clock = FakeClock()
    assembly = MemoryAssembly(
        clock=clock,
        root=tmp_path,
        use_real_eval=True,
        use_real_integrator=True,
        durable=True,
    )
    first = prepare(assembly, clock, tmp_path)
    first.supervisor.tick(clock.now_seconds())
    submit_on_a_task(
        first, WORKER_ONE, write_candidate(tmp_path / "a", WORKER_ONE, {"A": 2.0}, 0)
    )
    held = first.components.task_queue.claim_task(WORKER_TWO)
    assert held is not None
    first.components.leases.acquire(WORKER_TWO, held, "handle-held")
    for _ in range(2):
        clock.advance(TOY_SOURCE.latency_seconds + 1.0)
        first.components.eval_service.tick()
    assert first.components.integrator.current().k == 0, "verified, not integrated"
    first.supervisor.stop_signal.set()

    second = prepare(assembly, clock, tmp_path)
    assert second.components.leases.active() == ()
    assert queued_task_ids(second).count(held.id) == 1, "requeued exactly once"
    judge_and_integrate(second, clock)
    assert second.components.integrator.current().k == 1
    assert queued_task_ids(second).count(held.id) == 1
    second.stop()
    assert_each_event_once(second.components.blackboard)


def test_a_resumed_run_keeps_the_critics_rechecks_and_refutations(
    tmp_path: Path,
) -> None:
    """The Integrator rebuilt its queue from every claim verified at the top level, so
    a jump the Critic had refuted would have merged after a restart (#120, D66)."""
    clock = FakeClock()
    params = Params(slots_per_approach={TOY_APPROACH: 2}, quota_integrator_per_hour=100)
    assembly = MemoryAssembly(
        clock=clock,
        root=tmp_path,
        use_real_eval=True,
        use_real_integrator=True,
        use_critic=True,
        durable=True,
    )
    first = prepare(assembly, clock, tmp_path, params)
    first.components.eval_service.request(
        EvalRequest(
            candidate_directory=write_candidate(
                tmp_path / "lucky", WORKER_ONE, {"LUCKY": 3.0}, 0, audit_score=0.0
            ),
            predictions={TOY_SOURCE.id: 3.0},
            rationale="a lucky jump",
        ),
        WORKER_ONE,
    )
    judge_and_integrate(first, clock, ticks=6)
    critic = first.components.critic
    assert critic is not None and critic.recheck_count == 1
    first.supervisor.stop_signal.set()

    second = prepare(assembly, clock, tmp_path, params)
    resumed_critic = second.components.critic
    assert resumed_critic is not None and resumed_critic.recheck_count == 1
    second.components.eval_service.request(
        EvalRequest(
            candidate_directory=write_candidate(
                tmp_path / "honest", WORKER_TWO, {"HONEST": 1.0}, 0
            ),
            predictions={TOY_SOURCE.id: 1.0},
            rationale="an honest jump",
        ),
        WORKER_TWO,
    )
    judge_and_integrate(second, clock, ticks=6)
    assert resumed_critic.recheck_count == 2, "the honest jump is rechecked too"
    second.stop()
    deliverable = second.report().deliverable
    assert (deliverable.k, len(deliverable.candidates)) == (1, 1), "lucky stays out"
    assert_each_event_once(second.components.blackboard)
