"""The Critic's holdouts: jump alerts on the spot-check split, the shared cap, and
the sealed verdict (D34, D66, D72)."""

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tests.fixtures.eval_harness import WORKER, EvalHarness, make_eval_harness
from tests.fixtures.herd import TOY_APPROACH, TOY_SEED_COUNT, TOY_SOURCE
from tests.fixtures.runtime import (
    FakeIntegrator,
    MemoryAssembly,
    WaitingRunner,
    random_for_tests,
    toy_problem,
)
from vlah.adapters.memory.model import MemoryModel
from vlah.adapters.memory.signal_source import FAKE_SCORE_FILE_NAME
from vlah.adapters.memory.store import MemoryVectorIndex
from vlah.arms.config import PRESETS
from vlah.core.eval.api import EvalRequest, RemeasureRequest
from vlah.core.indexer.indexer import StoreIndexer
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.candidates import CandidateManifest, candidate_hash
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import (
    AuditChecked,
    BaselineReverted,
    RemeasureReady,
    event_from_json,
)
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Limits, Params
from vlah.core.types.sources import ScoreResult
from vlah.ports.signal_source import Dataset
from vlah.roles.critic.holdout import HoldoutCritic, SealedVerdict
from vlah.runtime.command import write_report
from vlah.runtime.herd import HerdRun, build_herd, events_on
from vlah.runtime.status import read_status

pytestmark = pytest.mark.critic

TOP_LEVEL = "slow"
JUDGE_SECONDS = 1200.0
# Paired against the harness's all-zero baseline: a mean of 1.0 with a standard
# error of 0.07 is a jump; 0.1 with the same error is not.
JUMP = (1.0, 1.2, 0.9, 1.1, 0.8)
FLAT = (0.0, 0.1, -0.1, 0.05, -0.05)
SMALL_GAIN = (0.1, 0.3, -0.1, 0.2, 0.0)
DELIVERABLE = BaselineRecord(k=2, parent=1, candidates=("c1", "c2"), created_ts=0.0)
RUN_TICKS_PER_CANDIDATE = 6
RUN_SECONDS = 86400.0


def scored_candidate(
    harness: EvalHarness,
    name: str,
    daily: tuple[float, ...],
    audit: tuple[float, ...],
) -> Path:
    directory = harness.candidate(0.0, name=name)
    (directory / FAKE_SCORE_FILE_NAME).write_text(
        json.dumps({"per_seed": list(daily), "audit_per_seed": list(audit)}),
        encoding="utf-8",
    )
    return directory


def verified_claim(harness: EvalHarness, critic: HoldoutCritic, directory: Path) -> str:
    """Run a worker's candidate up the ladder and hand its claim to the Critic, as
    the fan-out does when the claim is verified at the top level."""
    response = harness.service.request(harness.request(directory), WORKER)
    assert response.status == "queued" and response.claim_id is not None
    harness.run(seconds=JUDGE_SECONDS)
    critic.on_verified(response.claim_id)
    return response.claim_id


def make_critic(
    harness: EvalHarness,
    work_directory: Path,
    params: Params,
    verdict: SealedVerdict | None = None,
) -> tuple[HoldoutCritic, FakeIntegrator]:
    integrator = FakeIntegrator(params=params)
    critic = HoldoutCritic(
        blackboard=harness.blackboard,
        eval_service=harness.service,
        integrator=integrator,
        arm=PRESETS["S2"],
        params=params,
        clock=harness.clock,
        top_level=TOP_LEVEL,
        work_directory=work_directory,
        verdict=verdict,
    )
    return critic, integrator


def test_a_lucky_jump_is_refuted_and_an_honest_one_goes_on(tmp_path: Path) -> None:
    """High on the daily set and flat on the spot-check split is a lucky draw; the
    same gain on both is honest; a gain inside the noise is never rechecked."""
    harness = make_eval_harness(tmp_path / "eval")
    critic, integrator = make_critic(harness, tmp_path / "critic", Params())
    small = verified_claim(
        harness, critic, scored_candidate(harness, "small", SMALL_GAIN, SMALL_GAIN)
    )
    honest = verified_claim(
        harness, critic, scored_candidate(harness, "honest", JUMP, JUMP)
    )
    lucky = verified_claim(
        harness, critic, scored_candidate(harness, "lucky", JUMP, FLAT)
    )
    critic.tick()
    assert integrator.verified == [small], "both jumps wait for their recheck"
    harness.run(seconds=JUDGE_SECONDS)
    written = critic.tick()
    assert integrator.verified == [small, honest], "the lucky claim never goes on"
    checks = {event.target: event for event in written if event.type == "audit_checked"}
    flagged = {
        claim: event.data.flagged
        for claim, event in checks.items()
        if isinstance(event.data, AuditChecked)
    }
    assert flagged == {honest: False, lucky: True}
    (refutation,) = [event for event in written if event.type == "claim_status"]
    assert (refutation.target, refutation.from_, refutation.to) == (
        lucky,
        "verified:slow",
        "refuted",
    )
    assert (refutation.actor, refutation.role) == ("critic-01", "critic")
    assert refutation.evidence[0] == f"events/{checks[lucky].local_id}.json"
    measurement = event_from_json(
        harness.blackboard.read(refutation.evidence[1], None).decode()
    )
    lucky_check = checks[lucky].data
    assert isinstance(measurement.data, RemeasureReady)
    assert isinstance(lucky_check, AuditChecked)
    assert measurement.data.dataset == "audit"
    assert measurement.data.paired == lucky_check.audit
    audit_jobs = [
        job
        for source in harness.sources
        for job in source.jobs.values()
        if job.dataset == "audit"
    ]
    assert len(audit_jobs) == 2, "no recheck for a gain inside the noise"
    indexer = StoreIndexer(
        blackboard=harness.blackboard,
        vectors=MemoryVectorIndex(),
        model=MemoryModel(),
        params=Params(),
        clock=harness.clock,
        top_level=TOP_LEVEL,
    )
    indexer.rebuild()
    assert indexer.claim_state(lucky).status == "refuted", "a legal D9 transition"
    assert indexer.claim_state(honest).status == "verified:slow"


def test_a_restarted_critic_counts_its_earlier_rechecks(tmp_path: Path) -> None:
    """A Critic started again over the run's blackboard must not spend its share
    twice: the Integrator's reserved checks depend on it."""
    harness = make_eval_harness(tmp_path / "eval")
    params = Params(holdout_run_cap=4)
    first, _ = make_critic(harness, tmp_path / "critic", params)
    verified_claim(harness, first, scored_candidate(harness, "lucky", JUMP, FLAT))
    first.tick()
    harness.run(seconds=JUDGE_SECONDS)
    first.tick()
    restarted, integrator = make_critic(harness, tmp_path / "critic", params)
    jump = verified_claim(
        harness, restarted, scored_candidate(harness, "jump", JUMP, JUMP)
    )
    restarted.tick()
    assert integrator.verified == [jump], "its one recheck is spent"


@dataclass(frozen=True, kw_only=True)
class JudgeRun:
    dataset: Dataset
    baseline: str


@dataclass(kw_only=True)
class ScriptedVerdictJudge:
    values: tuple[float | None, ...]
    runs: list[JudgeRun] = field(default_factory=list[JudgeRun])

    def measure_directory(
        self, directory: Path, dataset: Dataset, baseline: str
    ) -> ScoreResult:
        self.runs.append(JudgeRun(dataset=dataset, baseline=baseline))
        return ScoreResult(value=self.values[len(self.runs) - 1], n=1)


@dataclass(kw_only=True)
class RecordedHoldout:
    scores: list[float] = field(default_factory=list[float])

    def record_holdout(self, score: float) -> None:
        self.scores.append(score)


def test_the_verdict_split_is_sealed_during_the_run_and_used_once_after(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        RemeasureRequest.model_validate(
            {
                "candidate_directory": tmp_path,
                "baseline": "baseline@0",
                "parent_candidate": "baseline@0",
                "approach": TOY_APPROACH,
                "dataset": "judge",
            }
        )
    params = Params()
    judge = ScriptedVerdictJudge(values=(0.6, 0.7, None, 0.5, 0.6))
    recorded = RecordedHoldout()
    critic, _ = make_critic(
        make_eval_harness(tmp_path / "eval"),
        tmp_path / "critic",
        params,
        verdict=SealedVerdict(
            judge=judge,
            repeat_count=params.verdict_repeat_count,
            deliverable_directory=lambda deliverable: tmp_path / "deliverable",
            metrics=recorded,
        ),
    )
    with pytest.raises(RuntimeError, match="sealed"):
        critic.score_verdict(DELIVERABLE)
    assert judge.runs == []
    critic.stop()
    assert critic.score_verdict(DELIVERABLE) == pytest.approx(0.6)
    assert (
        judge.runs == [JudgeRun(dataset="judge", baseline="baseline@2")] * 5
    ), "n is fixed: the void run is not run again"
    assert recorded.scores == [pytest.approx(0.6)]
    with pytest.raises(RuntimeError, match="once"):
        critic.score_verdict(DELIVERABLE)
    assert len(judge.runs) == 5


def chained_candidate(
    root: Path, name: str, target: int, daily: float, audit: float
) -> Path:
    """A whole payload on baseline@target: every earlier change plus its own, with
    the scores the fake judge gives the merged result on every seed."""
    directory = root / name
    directory.mkdir(parents=True)
    for number in range(target + 1):
        (directory / f"change-{number}.txt").write_text(f"{name}\n", encoding="utf-8")
    (directory / FAKE_SCORE_FILE_NAME).write_text(
        json.dumps(
            {
                "per_seed": [daily] * TOY_SEED_COUNT,
                "audit_per_seed": [audit] * TOY_SEED_COUNT,
            }
        ),
        encoding="utf-8",
    )
    manifest = CandidateManifest(
        kind="artifact",
        target=f"baseline@{target}",
        approach=TOY_APPROACH,
        author=ActorId("worker-01"),
        created_at=0.0,
    )
    (directory / "manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8"
    )
    return directory


def submit_and_settle(run: HerdRun, clock: FakeClock, directory: Path) -> str:
    """Send a worker's candidate and tick the run until its fate is decided."""
    response = run.components.eval_service.request(
        EvalRequest(
            candidate_directory=directory,
            predictions={TOY_SOURCE.id: 1.0},
            rationale="it should help",
        ),
        ActorId("worker-01"),
    )
    assert response.status == "queued"
    for _ in range(RUN_TICKS_PER_CANDIDATE):
        run.tick(clock.now_seconds())
        clock.advance(TOY_SOURCE.latency_seconds + 1.0)
    manifest_data: object = yaml.safe_load(
        (directory / "manifest.yaml").read_text(encoding="utf-8")
    )
    return candidate_hash(
        directory, CandidateManifest.model_validate(manifest_data).target
    )


def test_with_the_cap_at_four_the_critic_rechecks_once_and_the_integrator_keeps_three(
    tmp_path: Path,
) -> None:
    """The Critic's one recheck refutes the first lucky jump; the second goes on
    unchecked and merges; the closing spot check and its bisection then use the
    Integrator's three reserved checks, all of which the eval client sends, and
    revert it. The deliverable's verdict lands in the report."""
    clock = FakeClock()
    params = Params(
        slots_per_approach={TOY_APPROACH: 1},
        holdout_run_cap=4,
        quota_integrator_per_hour=100,
    )
    problem = dataclasses.replace(
        toy_problem(tmp_path, worker_count=1),
        params=params,
        limits=Limits(eval_machine_hours=100.0, days=1.0),
    )
    assembly = MemoryAssembly(
        clock=clock,
        root=tmp_path,
        use_real_eval=True,
        use_real_integrator=True,
        use_critic=True,
    )
    run = build_herd(
        problem,
        "S2",
        assembly,
        "r-1",
        root=tmp_path,
        worker_count=1,
        duration_seconds=RUN_SECONDS,
        clock=clock,
        random=random_for_tests(),
        runner=WaitingRunner(),
    ).prepare()
    candidates = tmp_path / "candidates"
    first_lucky = submit_and_settle(
        run, clock, chained_candidate(candidates, "lucky-0", 0, 3.0, 0.0)
    )
    honest = [
        submit_and_settle(
            run,
            clock,
            chained_candidate(
                candidates, f"honest-{number}", number, number + 1.0, number + 1.0
            ),
        )
        for number in range(4)
    ]
    second_lucky = submit_and_settle(
        run, clock, chained_candidate(candidates, "lucky-4", 4, 7.0, 4.0)
    )
    assert run.status().current_baseline == 5, "every honest change and the second jump"
    run.stop()
    events = list(events_on(run.components.blackboard))
    critic_checks = [event for event in events if event.actor == "critic-01"]
    assert [event.type for event in critic_checks] == ["audit_checked", "claim_status"]
    assert critic_checks[1].to == "refuted"
    audit_measurements = [
        event.data
        for event in events
        if isinstance(event.data, RemeasureReady) and event.data.dataset == "audit"
    ]
    assert [ready.parent_candidate for ready in audit_measurements] == [
        first_lucky,
        "baseline@5",
        "baseline@2",
        "baseline@3",
    ], "one recheck, then the closing check and two bisection steps"
    (revert,) = [
        event.data for event in events if isinstance(event.data, BaselineReverted)
    ]
    assert second_lucky in revert.removed
    report = run.report()
    assert report.deliverable.candidates == tuple(honest[:3])
    assert report.metrics.holdout_score == 3.0
    assert [run.dataset for run in assembly.verdict_judge.runs] == ["judge"] * 5
    write_report(report, tmp_path / "report.json")
    report_file = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report_file["holdout_score"] == 3.0
    status = read_status(run.status_path)
    assert (status.deliverable_baseline, status.closeout_error) == (
        report.deliverable.k,
        None,
    ), "the status file is written through the Critic's closeout too"
