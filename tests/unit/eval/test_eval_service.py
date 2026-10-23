"""The eval client: validation, the ladder, confirmation, dedup, gates, remeasures (v2.2 section 9)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.fixtures.eval_harness import WORKER, make_eval_harness, source_declaration
from vlah.adapters.memory.model import MemoryModel
from vlah.adapters.memory.signal_source import FakeSlowSource
from vlah.adapters.memory.store import MemoryVectorIndex
from vlah.arms.config import PRESETS, ArmName
from vlah.core.eval.api import RemeasureRequest
from vlah.core.eval.jobs import GateOutcome
from vlah.core.indexer.api import Hit
from vlah.core.indexer.indexer import StoreIndexer
from vlah.core.types.candidates import payload_text
from vlah.core.types.events import RemeasureReady, ResultReady
from vlah.core.types.ids import ActorId
from vlah.core.types.params import GateSpec, GateStage, Params
from vlah.core.types.sources import ScoreResult, SourceDecl
from vlah.ports.errors import JudgeBusyError
from vlah.ports.signal_source import CandidateRef, Dataset

pytestmark = pytest.mark.eval

OTHER = ActorId("worker-08")
COMPOSE = ActorId("worker-00")
CHEAP = source_declaration("cheap", 0.1, "2m")
EXPENSIVE = source_declaration("expensive", 10.0, "6h")


@pytest.mark.parametrize(
    ("fields", "error_part"),
    [
        ({"predictions": {}}, "predictions are required"),
        ({"rationale": "  "}, "rationale"),
        ({"parents": ["deadbeef"]}, "unknown parent"),
    ],
)
def test_invalid_requests_are_refused_before_anything_is_written(
    tmp_path: Path, fields: dict[str, object], error_part: str
) -> None:
    harness = make_eval_harness(tmp_path)
    request = harness.request(harness.candidate(1.0)).model_copy(update=fields)
    response = harness.service.request(request, WORKER)
    assert response.status == "invalid_request"
    assert response.error is not None and error_part in response.error
    assert harness.blackboard.list_paths("", None) == ()


def test_ladder_climbs_by_cost_and_stops_when_a_level_shows_no_gain(
    tmp_path: Path,
) -> None:
    harness = make_eval_harness(tmp_path, declarations=(EXPENSIVE, CHEAP))
    winner = harness.service.request(harness.request(harness.candidate(2.0)), WORKER)
    loser = harness.service.request(harness.request(harness.candidate(-1.0)), WORKER)
    events = harness.run(seconds=8 * 3600)
    statuses = [
        (event.target, event.to) for event in events if event.type == "claim_status"
    ]
    assert (winner.claim_id, "verified:cheap") in statuses
    assert (winner.claim_id, "verified:expensive") in statuses
    assert (loser.claim_id, "verified:cheap") in statuses
    assert (
        loser.claim_id,
        "verified:expensive",
    ) not in statuses, "no gain, no promotion"
    results = {
        event.data.claim_id: event.data
        for event in events
        if event.type == "result_ready" and isinstance(event.data, ResultReady)
    }
    assert winner.claim_id is not None and loser.claim_id is not None
    assert results[winner.claim_id].level == "expensive"
    assert results[loser.claim_id].level == "cheap"
    assert len(results) == 2, "one result_ready per request, when its ladder ends"


def test_a_confirm_token_works_once_and_only_for_its_caller(tmp_path: Path) -> None:
    harness = make_eval_harness(tmp_path)
    harness.indexer.candidate_hits = (
        Hit(
            item_id="c-old",
            kind="candidate",
            similarity=0.97,
            summary="almost the same",
        ),
    )
    directory = harness.candidate(1.0)
    first = harness.service.request(harness.request(directory), WORKER)
    assert first.status == "needs_confirmation"
    with_token = harness.request(directory, confirm_token=first.confirm_token)
    assert (
        harness.service.request(with_token, ActorId("worker-08")).status
        == "invalid_request"
    )
    second = harness.service.request(harness.request(directory), WORKER)
    queued = harness.service.request(
        harness.request(directory, confirm_token=second.confirm_token), WORKER
    )
    assert queued.status == "queued"
    reused = harness.service.request(
        harness.request(harness.candidate(2.0), confirm_token=second.confirm_token),
        WORKER,
    )
    assert reused.status == "invalid_request", "a used token is gone"


def test_b1_runs_duplicates_and_asks_nothing(tmp_path: Path) -> None:
    """B1 has no sharing: another worker's duplicate really runs and is billed (D58)."""
    harness = make_eval_harness(tmp_path, arm=PRESETS["B1"])
    harness.indexer.candidate_hits = (
        Hit(item_id="c-old", kind="candidate", similarity=0.99, summary="same"),
    )
    directory = harness.candidate(1.0)
    first = harness.service.request(harness.request(directory), WORKER)
    second = harness.service.request(harness.request(directory), OTHER)
    assert (first.status, second.status) == ("queued", "queued")
    harness.run(seconds=3600)
    assert len(harness.sources[0].jobs) == 2
    assert len(harness.budget.records) == 2


def test_a_cache_hit_costs_neither_quota_nor_budget(tmp_path: Path) -> None:
    """The judge answered a repeat from its cache, so no run was spent (TB section 11.3)."""
    harness = make_eval_harness(
        tmp_path,
        arm=PRESETS["B1"],
        declarations=(source_declaration("slow", 1.0, "10m", per_day=2),),
    )
    directory = harness.candidate(1.0)
    harness.service.request(harness.request(directory), WORKER)
    harness.service.request(harness.request(directory), WORKER)
    harness.run(seconds=3600)
    assert len(harness.budget.records) == 1, "the repeat was not run"
    third = harness.service.request(harness.request(harness.candidate(2.0)), WORKER)
    events = harness.run(seconds=3600)
    assert any(
        event.type == "result_ready" and event.target == third.claim_id
        for event in events
    ), "the refunded slot lets a third candidate run the same day"


def test_the_worker_is_the_actor_and_the_bytes_are_unchanged(tmp_path: Path) -> None:
    """The quota-discipline metric matches submissions to tb fast runs by hash (TB section 11.5)."""
    harness = make_eval_harness(tmp_path)
    directory = harness.candidate(1.0)
    original_bytes = {path.name: path.read_bytes() for path in directory.iterdir()}
    harness.service.request(harness.request(directory), WORKER)
    harness.run(seconds=3600)
    (job,) = harness.sources[0].jobs.values()
    assert job.actor == WORKER
    assert {
        path.name: path.read_bytes() for path in directory.iterdir()
    } == original_bytes


@dataclass(kw_only=True)
class BusyOnceSource:
    """A judge that answers its first submission with "busy until 1800 s"."""

    inner: FakeSlowSource
    submit_times: list[float] = field(default_factory=list[float])

    @property
    def declaration(self) -> SourceDecl:
        return self.inner.declaration

    def submit(
        self, candidate: CandidateRef, actor: ActorId, baseline: str, dataset: Dataset
    ) -> str:
        self.submit_times.append(self.inner.clock.now_seconds())
        if len(self.submit_times) == 1:
            raise JudgeBusyError(retry_at_seconds=1800.0)
        else:
            return self.inner.submit(candidate, actor, baseline, dataset)

    def poll(self, job_id: str) -> ScoreResult | None:
        return self.inner.poll(job_id)


def test_a_busy_judge_is_retried_at_the_time_it_gives(tmp_path: Path) -> None:
    harness = make_eval_harness(tmp_path)
    busy_source = BusyOnceSource(inner=harness.sources[0])
    harness.service.ladder = (busy_source,)
    harness.service.request(harness.request(harness.candidate(1.0)), WORKER)
    harness.run(seconds=7200, steps=8)
    assert busy_source.submit_times[0] == 0.0
    assert busy_source.submit_times[1] >= 1800.0
    assert len(harness.sources[0].jobs) == 1


def test_constraint_failure_refutes_and_records_a_dead_end(tmp_path: Path) -> None:
    harness = make_eval_harness(tmp_path)
    response = harness.service.request(
        harness.request(harness.candidate(5.0, constraint_ok=False)), WORKER
    )
    events = harness.run(seconds=3600)
    assert (response.claim_id, "refuted") in [
        (event.target, event.to) for event in events if event.type == "claim_status"
    ]
    dead_ends = harness.blackboard.list_paths("claims/eval_service/", None)
    assert any(
        b"gate:constraint" in harness.blackboard.read(path, None) for path in dead_ends
    )


@pytest.mark.parametrize("stage", ["pre_submit", "post_result"])
def test_a_gate_dead_end_is_confirmed_so_the_presearch_warns_about_it(
    tmp_path: Path, stage: GateStage
) -> None:
    """D54 wants gate failures in the pre-submit search, which covers confirmed dead ends only (D26)."""
    harness = make_eval_harness(
        tmp_path, gates=(GateSpec(name="unit_tests", command="pytest", when=(stage,)),)
    )
    harness.gates.failing.add("unit_tests")
    directory = harness.candidate(1.0)
    harness.service.request(harness.request(directory), WORKER)
    harness.run(seconds=3600)
    indexer = StoreIndexer(
        blackboard=harness.blackboard,
        vectors=MemoryVectorIndex(),
        model=MemoryModel(),
        params=Params(),
        clock=harness.clock,
        top_level="slow",
    )
    indexer.rebuild()
    (confirmation,) = [
        event
        for event in harness.events_on_blackboard()
        if event.type == "claim_status" and event.to == "confirmed"
    ]
    assert confirmation.actor == "eval_service" and confirmation.target is not None
    assert indexer.claim_state(confirmation.target).status == "confirmed"
    hits = indexer.similar_dead_ends(payload_text(directory), 5, OTHER)
    assert [hit.item_id for hit in hits] == [confirmation.target]


def test_remeasure_runs_once_at_the_top_level_for_the_integrator(
    tmp_path: Path,
) -> None:
    """D74: one paired remeasure at the slow tier, outside dedup and confirmation."""
    harness = make_eval_harness(tmp_path, declarations=(CHEAP, EXPENSIVE))
    original = harness.service.request(
        harness.request(harness.candidate(2.0, name="c")), WORKER
    )
    harness.run(seconds=8 * 3600)
    harness.indexer.candidate_hits = (
        Hit(item_id="c", kind="candidate", similarity=1.0, summary="the original"),
    )
    rebased = harness.candidate(1.5, name="rebased")
    request_id = harness.service.remeasure(
        RemeasureRequest(
            candidate_directory=rebased,
            baseline="baseline@0",
            parent_candidate=original.request_id,
            approach="a-01",
        )
    )
    events = harness.run(seconds=8 * 3600)
    remeasures = [
        event.data for event in events if isinstance(event.data, RemeasureReady)
    ]
    assert [remeasure.request_id for remeasure in remeasures] == [request_id]
    assert remeasures[0].level == "expensive"
    assert remeasures[0].paired is not None and remeasures[0].paired.mean == 1.5
    integrator_jobs = [
        job
        for source in harness.sources
        for job in source.jobs.values()
        if job.actor == "integrator"
    ]
    assert [job.job_id.split("-")[0] for job in integrator_jobs] == ["expensive"]
    assert not any(event.type == "result_ready" for event in events)


@pytest.mark.parametrize("arm", ["B1", "S1", "S2"])
def test_a_compose_request_runs_the_same_in_every_arm(
    tmp_path: Path, arm: ArmName
) -> None:
    """The compose slot's request went through the arm's dedup and confirmation, so
    the same compose candidate ran in B1 and got dedup_hit in S1 and S2 (#56 R5, D74).
    """
    harness = make_eval_harness(
        tmp_path, arm=PRESETS[arm], declarations=(CHEAP, EXPENSIVE)
    )
    directory = harness.candidate(2.0)
    original = harness.service.request(harness.request(directory), WORKER)
    harness.run(seconds=8 * 3600)
    harness.indexer.candidate_hits = (
        Hit(item_id="c-old", kind="candidate", similarity=1.0, summary="same"),
    )
    harness.indexer.dead_end_hits = (
        Hit(item_id="c-dead", kind="claim", similarity=1.0, summary="tried"),
    )
    composed = harness.service.request(
        harness.request(directory, kind="compose"), COMPOSE
    )
    assert composed.status == "queued", "no dedup and no confirmation for compose"
    events = harness.run(seconds=8 * 3600)
    (ready,) = [event.data for event in events if isinstance(event.data, ResultReady)]
    assert (ready.submitter, ready.claim_id, ready.level) == (
        COMPOSE,
        composed.claim_id,
        "expensive",
    ), "a compose result is a result_ready for the compose slot, not a remeasure"
    quota = harness.service.runner.quota
    assert len(quota.integrator_runs) == 2, "both levels on the Integrator's quota"
    assert COMPOSE not in quota.cheap_runs_by_worker
    assert len(quota.runs_by_level["cheap"]) == 1, "only the worker's run"
    repeat = harness.service.request(harness.request(directory), OTHER)
    assert (repeat.status == "dedup_hit") == PRESETS[arm].hash_dedup
    assert repeat.claim_id != composed.claim_id, "never the compose claim"
    if PRESETS[arm].hash_dedup:
        assert repeat.claim_id == original.claim_id, "the worker's claim"
    else:
        pass


def test_a_restart_restores_compose_runs_to_the_integrator_quota(
    tmp_path: Path,
) -> None:
    """Replayed as worker runs, a compose candidate's runs would charge the worker
    quota after a restart and become a dedup original (D13, D74)."""
    harness = make_eval_harness(tmp_path, declarations=(CHEAP, EXPENSIVE))
    harness.service.request(
        harness.request(harness.candidate(2.0), kind="compose"), COMPOSE
    )
    harness.run(seconds=8 * 3600)
    restarted = make_eval_harness(
        tmp_path, declarations=(CHEAP, EXPENSIVE), blackboard=harness.blackboard
    )
    assert len(restarted.service.runner.quota.integrator_runs) == 2
    assert COMPOSE not in restarted.service.runner.quota.cheap_runs_by_worker
    assert restarted.service.claims_by_hash == {}


def test_post_result_gates_refute(tmp_path: Path) -> None:
    harness = make_eval_harness(
        tmp_path,
        gates=(GateSpec(name="latency", command="check", when=("post_result",)),),
    )
    harness.gates.failing.add("latency")
    response = harness.service.request(harness.request(harness.candidate(3.0)), WORKER)
    events = harness.run(seconds=3600)
    assert (response.claim_id, "refuted") in [
        (event.target, event.to) for event in events if event.type == "claim_status"
    ]


@dataclass(kw_only=True)
class OverlapCountingGates:
    """A slow gate that records how many requests were inside the service at once."""

    hold_seconds: float = 0.02
    inside: int = 0
    most_inside: int = 0
    counter_lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, gate: GateSpec, candidate_directory: Path) -> GateOutcome:
        with self.counter_lock:
            self.inside += 1
            self.most_inside = max(self.most_inside, self.inside)
        time.sleep(self.hold_seconds)
        with self.counter_lock:
            self.inside -= 1
        return GateOutcome(passed=True, output="ran")


def test_concurrent_duplicates_are_billed_once(tmp_path: Path) -> None:
    """The tool server runs each worker's eval_request on its own thread (#16)."""
    calls = 4
    harness = make_eval_harness(
        tmp_path, gates=(GateSpec(name="build", command="make", when=("pre_submit",)),)
    )
    gates = OverlapCountingGates()
    harness.service.runner.gate_runner = gates
    directory = harness.candidate(0.5)
    with ThreadPoolExecutor(max_workers=calls) as pool:
        statuses = sorted(
            response.status
            for response in pool.map(
                lambda _: harness.service.request(harness.request(directory), WORKER),
                range(calls),
            )
        )
    assert statuses == ["dedup_hit"] * (calls - 1) + ["queued"]
    assert gates.most_inside == 1
