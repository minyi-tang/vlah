"""The eval client under judge trouble, restarts and concurrency (#42: B3, B4, B5, S2, S6, S7, S8)."""

import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

import pytest

from tests.fixtures.eval_harness import (
    WORKER,
    EvalHarness,
    make_eval_harness,
    source_declaration,
    zero_baseline,
)
from vlah.adapters.memory.signal_source import FakeSlowSource
from vlah.arms.config import PRESETS
from vlah.core.eval.api import RemeasureRequest
from vlah.core.eval.jobs import GateOutcome
from vlah.core.eval.ladder import MAX_JUDGE_RETRIES
from vlah.core.eval.service import LadderEvalService
from vlah.core.indexer.api import Hit
from vlah.core.types.events import Event, ResultReady
from vlah.core.types.ids import ActorId
from vlah.core.types.params import GateSpec, Params
from vlah.core.types.sources import ScoreResult, SourceDecl
from vlah.ports.signal_source import CandidateRef, Dataset

pytestmark = pytest.mark.eval

OTHER = ActorId("worker-08")


@dataclass(kw_only=True)
class ScriptedJudge:
    """A judge that fails as told before handing over to the fake one."""

    inner: FakeSlowSource
    failing_names: dict[str, Exception] = field(default_factory=dict[str, Exception])
    transient_failures: int = 0
    submitted: list[CandidateRef] = field(default_factory=list[CandidateRef])

    @property
    def declaration(self) -> SourceDecl:
        return self.inner.declaration

    def submit(
        self, candidate: CandidateRef, actor: ActorId, baseline: str, dataset: Dataset
    ) -> str:
        self.submitted.append(candidate)
        for name, error in self.failing_names.items():
            if name in str(candidate.payload_directory):
                raise error
            else:
                continue
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise ConnectionError("the judge did not answer")
        else:
            return self.inner.submit(candidate, actor, baseline, dataset)

    def poll(self, job_id: str) -> ScoreResult | None:
        return self.inner.poll(job_id)


def scripted(harness: EvalHarness) -> ScriptedJudge:
    judge = ScriptedJudge(inner=harness.sources[0])
    harness.service.ladder = (judge,)
    for job in harness.service.jobs:
        job.levels = (judge,)
    return judge


def results(events: list[Event]) -> list[ResultReady]:
    return [event.data for event in events if isinstance(event.data, ResultReady)]


def test_a_judge_failure_on_one_job_does_not_stall_the_others(tmp_path: Path) -> None:
    harness = make_eval_harness(tmp_path)
    judge = scripted(harness)
    judge.failing_names["bad"] = ValueError("the slow tier refused it (400)")
    bad = harness.request(harness.candidate(1.0, name="bad"))
    harness.service.request(bad, WORKER)
    harness.service.request(
        harness.request(harness.candidate(2.0, name="good")), WORKER
    )
    (failed, measured) = sorted(
        results(harness.run(seconds=3600)), key=lambda result: result.score is None
    )[::-1]
    assert (failed.score, failed.paired, failed.constraint_ok) == (None, None, None)
    assert measured.score == 2.0 and measured.constraint_ok is True
    judge.failing_names.clear()
    again = harness.service.request(bad, WORKER)
    assert again.status == "queued", "a judge failure releases the hash"


def test_transient_judge_errors_back_off_and_then_succeed(tmp_path: Path) -> None:
    harness = make_eval_harness(tmp_path)
    judge = scripted(harness)
    judge.transient_failures = 2
    harness.service.request(harness.request(harness.candidate(2.0)), WORKER)
    harness.service.tick()
    (job,) = harness.service.jobs
    first_wait = job.retry_at_seconds - harness.clock.now_seconds()
    harness.clock.advance(first_wait)
    harness.service.tick()
    assert job.retry_at_seconds - harness.clock.now_seconds() > first_wait
    (result,) = results(harness.run(seconds=7200, steps=40))
    assert result.score == 2.0
    assert len(judge.submitted) == 3


def test_transient_errors_give_up_after_the_retries(tmp_path: Path) -> None:
    harness = make_eval_harness(tmp_path)
    judge = scripted(harness)
    judge.transient_failures = 100
    harness.service.request(harness.request(harness.candidate(2.0)), WORKER)
    (result,) = results(harness.run(seconds=24 * 3600, steps=200))
    assert result.score is None and result.constraint_ok is None
    assert len(judge.submitted) == MAX_JUDGE_RETRIES + 1


def test_a_void_run_refutes_without_a_score(tmp_path: Path) -> None:
    """A seed that crashed or timed out is a normal outcome (TB section 11.3)."""
    harness = make_eval_harness(tmp_path)
    response = harness.service.request(harness.request(harness.candidate(2.0)), WORKER)
    harness.service.tick()
    source = harness.sources[0]
    (fake_job,) = source.jobs.values()
    source.jobs[fake_job.job_id] = dataclasses.replace(
        fake_job,
        result=ScoreResult(
            value=None, n=5, constraint_ok=False, extra={"reasons": ["timeout"]}
        ),
    )
    events = harness.run(seconds=3600)
    (result,) = results(events)
    assert (result.score, result.paired, result.constraint_ok) == (None, None, False)
    assert (response.claim_id, "refuted") in [
        (event.target, event.to) for event in events if event.type == "claim_status"
    ]
    dead_ends = harness.blackboard.list_paths("claims/eval_service/", None)
    assert any(
        "tags: [gate:void]" in harness.blackboard.read(path, None).decode()
        or "- gate:void" in harness.blackboard.read(path, None).decode()
        for path in dead_ends
    )


@dataclass(kw_only=True)
class CountingGates:
    failing: set[str] = field(default_factory=set[str])
    calls: int = 0

    def __call__(self, gate: GateSpec, candidate_directory: Path) -> GateOutcome:
        self.calls += 1
        return GateOutcome(passed=gate.name not in self.failing, output="ran")


def test_a_post_result_gate_failure_is_reported_as_refuted(tmp_path: Path) -> None:
    """D54: a gate is a hard constraint; the worker must not be told it gained (L0-7 gap)."""
    harness = make_eval_harness(
        tmp_path,
        gates=(GateSpec(name="latency", command="check", when=("post_result",)),),
    )
    gates = CountingGates(failing={"latency"})
    harness.service.runner.gate_runner = gates
    harness.service.request(harness.request(harness.candidate(3.0)), WORKER)
    (result,) = results(harness.run(seconds=3600))
    assert result.constraint_ok is False
    assert result.paired is not None and result.paired.mean == 3.0, "a measurement"
    stored = harness.blackboard.read(f"candidates/{result.candidate}/result.json", None)
    assert '"status": "refuted"' in stored.decode()

    broken = make_eval_harness(
        tmp_path / "broken",
        gates=(GateSpec(name="latency", command="check", when=("post_result",)),),
    )
    broken.service.runner.gate_runner = gates
    gates.calls = 0
    directory = broken.candidate(3.0, constraint_ok=False)
    broken.service.request(broken.request(directory), WORKER)
    (judged,) = results(broken.run(seconds=3600))
    assert judged.constraint_ok is False
    assert gates.calls == 0, "no gate runs once the judge has refuted it"


def test_a_remeasure_predicts_the_parents_last_score(tmp_path: Path) -> None:
    harness = make_eval_harness(tmp_path)
    judge = scripted(harness)
    parent = harness.service.request(
        harness.request(harness.candidate(2.0, name="parent")), WORKER
    )
    harness.run(seconds=3600)
    (parent_job,) = harness.service.jobs
    assert parent.claim_id == parent_job.claim_id
    harness.service.remeasure(
        RemeasureRequest(
            candidate_directory=harness.candidate(1.5, name="rebased"),
            baseline="baseline@0",
            parent_candidate=parent_job.candidate_hash,
            approach="a-01",
        )
    )
    harness.service.tick()
    assert judge.submitted[-1].manifest.predicted_score == 2.0


def test_an_unknown_baseline_is_refused_before_anything_is_spent(
    tmp_path: Path,
) -> None:
    harness = make_eval_harness(tmp_path)
    directory = harness.candidate(1.0)
    refused = harness.service.request(
        harness.request(directory, baseline="baseline@42"), WORKER
    )
    assert refused.status == "invalid_request" and "unknown baseline" in str(
        refused.error
    )
    harness.service.tick()
    assert harness.sources[0].jobs == {} and harness.budget.records == []
    harness.blackboard.write(
        ActorId("integrator"),
        {"baseline/1.json": b'{"k": 1}'},
        "baseline 1",
    )
    queued = harness.service.request(harness.request(directory), WORKER)
    assert queued.baseline == "baseline@0", "the manifest's target, not the newest"


def test_a_symbolic_link_in_the_candidate_is_refused(tmp_path: Path) -> None:
    harness = make_eval_harness(tmp_path)
    directory = harness.candidate(1.0)
    (tmp_path / "herd.yaml").write_text("secret: true\n")
    (directory / "leak.yaml").symlink_to(tmp_path / "herd.yaml")
    response = harness.service.request(harness.request(directory), WORKER)
    assert response.status == "invalid_request" and "symbolic link" in str(
        response.error
    )
    assert not harness.blackboard.list_paths("candidates/", None)


def test_dedup_survives_a_restart_but_unfinished_work_is_released(
    tmp_path: Path,
) -> None:
    harness = make_eval_harness(tmp_path)
    finished_directory = harness.candidate(1.0, name="finished")
    finished = harness.service.request(harness.request(finished_directory), WORKER)
    harness.run(seconds=3600)
    pending_directory = harness.candidate(2.0, name="pending")
    harness.service.request(harness.request(pending_directory), WORKER)
    restarted = LadderEvalService(
        blackboard=harness.blackboard,
        sources=harness.sources,
        indexer=harness.indexer,
        budget=harness.budget,
        gates=(),
        gate_runner=harness.gates,
        baseline_scores=zero_baseline,
        arm=PRESETS["S2"],
        params=Params(),
        clock=harness.clock,
        random=Random(0),
    )
    duplicate = restarted.request(harness.request(finished_directory), WORKER)
    assert (duplicate.status, duplicate.claim_id) == ("dedup_hit", finished.claim_id)
    resubmitted = restarted.request(harness.request(pending_directory), WORKER)
    assert resubmitted.status == "queued"


def test_a_restarted_service_keeps_to_its_daily_quota(tmp_path: Path) -> None:
    """The quota is rebuilt from the blackboard, so a restart does not reset it (#42, S7)."""
    declaration = source_declaration("slow", 1.0, "10m", per_day=2)
    harness = make_eval_harness(tmp_path, declarations=(declaration,))
    harness.service.request(harness.request(harness.candidate(1.0)), WORKER)
    harness.service.request(harness.request(harness.candidate(2.0)), WORKER)
    harness.run(seconds=3600)
    restarted = LadderEvalService(
        blackboard=harness.blackboard,
        sources=harness.sources,
        indexer=harness.indexer,
        budget=harness.budget,
        gates=(),
        gate_runner=harness.gates,
        baseline_scores=zero_baseline,
        arm=PRESETS["S2"],
        params=Params(),
        clock=harness.clock,
        random=Random(0),
    )
    harness.service = restarted
    restarted.request(harness.request(harness.candidate(3.0)), WORKER)
    harness.run(seconds=3600)
    assert len(harness.sources[0].jobs) == 2, "two runs today already"
    harness.run(seconds=86400)
    assert len(harness.sources[0].jobs) == 3, "the next day has quota again"


def test_a_remeasure_never_becomes_the_dedup_original(tmp_path: Path) -> None:
    harness = make_eval_harness(tmp_path)
    directory = harness.candidate(1.0, name="same")
    original = harness.service.request(harness.request(directory), WORKER)
    harness.run(seconds=3600)
    harness.service.remeasure(
        RemeasureRequest(
            candidate_directory=directory,
            baseline="baseline@0",
            parent_candidate="x",
            approach="a-01",
        )
    )
    harness.run(seconds=3600)
    duplicate = harness.service.request(harness.request(directory), OTHER)
    assert duplicate.claim_id == original.claim_id


def test_a_stranger_cannot_spend_someone_elses_confirm_token(tmp_path: Path) -> None:
    harness = make_eval_harness(tmp_path)
    harness.indexer.candidate_hits = (
        Hit(item_id="c-old", kind="candidate", similarity=0.97, summary="s"),
    )
    directory = harness.candidate(1.0)
    first = harness.service.request(harness.request(directory), WORKER)
    stolen = harness.service.request(
        harness.request(directory, confirm_token=first.confirm_token), OTHER
    )
    assert stolen.status == "invalid_request"
    retry = harness.service.request(
        harness.request(directory, confirm_token=first.confirm_token), WORKER
    )
    assert retry.status == "queued"


@dataclass(kw_only=True)
class MeetingGates:
    """Every pre-submit gate waits until the others arrive, then until it is released."""

    barrier: threading.Barrier
    release: threading.Event = field(default_factory=threading.Event)

    def __call__(self, gate: GateSpec, candidate_directory: Path) -> GateOutcome:
        self.barrier.wait()
        self.release.wait(10.0)
        return GateOutcome(passed=True, output="ran")


def test_slow_gates_hold_up_neither_tick_nor_other_candidates(tmp_path: Path) -> None:
    """The lock covers bookkeeping only; gates and judge calls run outside it (S6)."""
    harness = make_eval_harness(
        tmp_path,
        gates=(GateSpec(name="build", command="make", when=("pre_submit",)),),
    )
    queued_directory = harness.candidate(1.0, name="queued")
    harness.service.request(harness.request(queued_directory), WORKER)
    gates = MeetingGates(barrier=threading.Barrier(3, timeout=10))
    harness.service.runner.gate_runner = gates
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [
            pool.submit(
                harness.service.request,
                harness.request(harness.candidate(2.0 + number, name=f"slow-{number}")),
                WORKER,
            )
            for number in range(2)
        ]
        gates.barrier.wait()
        harness.service.tick()
        assert harness.sources[0].jobs, "tick submitted while two gates were running"
        gates.release.set()
        assert [future.result().status for future in pending] == ["queued", "queued"]
