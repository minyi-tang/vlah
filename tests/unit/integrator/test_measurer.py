"""The core's measurer over the eval client: manifests, requests, and the remeasure_ready round trip (D74)."""

import json
from pathlib import Path
from random import Random

import pytest
import yaml

from tests.fixtures.fakes import FakeIndexer, RecordingBudget
from tests.fixtures.herd import (
    TOY_APPROACH,
    TOY_SOURCE,
    make_store_blackboard,
    passing_gate,
    zero_baseline_scores,
)
from tests.fixtures.integrator import RecordingEval, ScriptedGates, make_integrator
from tests.fixtures.worker import RecordingTaskQueue
from vlah.adapters.memory.signal_source import FAKE_SCORE_FILE_NAME, FakeSlowSource
from vlah.arms.config import PRESETS
from vlah.core.blackboard.api import MergedCommit
from vlah.core.eval.api import EvalRequest
from vlah.core.eval.service import LadderEvalService
from vlah.core.integrator.core_adapter import CoreCalibration, Measurement
from vlah.core.integrator.measurer import EvalMeasurer
from vlah.core.integrator.service import HerdIntegrator
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import IntegrationFailed, RemeasureReady, event_from_json
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params

pytestmark = pytest.mark.integrator

WORKER_ONE = ActorId("worker-01")
WORKER_TWO = ActorId("worker-02")
HUMAN = ActorId("human")
PARENT = "ab" * 32
JUDGE_SECONDS = TOY_SOURCE.latency_seconds + 1.0
SCORE = 2.0


def test_submit_writes_a_manifest_and_one_remeasure_request(tmp_path: Path) -> None:
    eval_service = RecordingEval()
    measurer = EvalMeasurer(eval_service=eval_service, clock=FakeClock())
    measurer.approach_by_hash[PARENT] = "a-07"
    (tmp_path / "a.txt").write_text("A\n", encoding="utf-8")
    ticket = measurer.submit(tmp_path, 3, "dev", "remeasure", [PARENT], False, 1.0)
    assert ticket == "request-00001"
    manifest_data: object = yaml.safe_load((tmp_path / "manifest.yaml").read_text())
    manifest = CandidateManifest.model_validate(manifest_data)
    assert manifest.target == "baseline@3"
    assert manifest.parents == (PARENT,)
    assert manifest.approach == "a-07"
    assert manifest.author == "integrator"
    (request,) = eval_service.requests
    assert request.candidate_directory == tmp_path
    assert (request.baseline, request.parent_candidate, request.approach) == (
        "baseline@3",
        PARENT,
        "a-07",
    )
    assert measurer.result(ticket) is None
    measurer.answer(Measurement(ticket, "deadbeef", [1.0], 1.0, True))
    assert measurer.result(ticket) is not None


def test_a_baseline_measurement_has_no_parent_and_the_integrator_approach(
    tmp_path: Path,
) -> None:
    eval_service = RecordingEval()
    measurer = EvalMeasurer(eval_service=eval_service, clock=FakeClock())
    measurer.submit(tmp_path, 4, "dev", "deliverable baseline@4", [], True, 0.0)
    (request,) = eval_service.requests
    assert request.parent_candidate == "baseline@4"
    assert request.approach == "integrator"


def test_an_audit_measurement_asks_for_the_audit_dataset(tmp_path: Path) -> None:
    eval_service = RecordingEval()
    measurer = EvalMeasurer(eval_service=eval_service, clock=FakeClock())
    measurer.submit(tmp_path, 1, "audit", "audit measurement", [], False, 0.0)
    measurer.submit(tmp_path, 1, "dev", "remeasure", [], False, 0.0)
    assert [request.dataset for request in eval_service.requests] == ["audit", "dev"]
    with pytest.raises(ValueError):
        measurer.submit(tmp_path, 1, "judge", "never", [], False, 0.0)


def write_candidate(directory: Path, author: ActorId, extra_file: str) -> Path:
    """A full copy of the baseline plus one file, with the shared fake score."""
    directory.mkdir(parents=True)
    (directory / "policy.py").write_text("SCORE = 0\n", encoding="utf-8")
    (directory / extra_file).write_text(f"{extra_file}\n", encoding="utf-8")
    (directory / FAKE_SCORE_FILE_NAME).write_text(
        json.dumps({"per_seed": [SCORE] * 5}), encoding="utf-8"
    )
    manifest = CandidateManifest(
        kind="patch",
        target="baseline@0",
        approach=TOY_APPROACH,
        author=author,
        created_at=0.0,
    )
    (directory / "manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8"
    )
    return directory


class VerifiedToIntegrator:
    """The runtime's fan-out rule for the Integrator, without the runtime."""

    def __init__(self, integrator: HerdIntegrator) -> None:
        self.integrator = integrator

    def __call__(self, merged_commit: MergedCommit) -> None:
        for event in merged_commit.events:
            if (
                event.type == "claim_status"
                and event.target is not None
                and event.to == f"verified:{TOY_SOURCE.id}"
            ):
                claim_text = self.integrator.blackboard.read(
                    f"claims/eval_service/{event.target}.md", None
                ).decode("utf-8")
                if "remeasure" in parse_claim(claim_text).tags:
                    continue
                else:
                    self.integrator.on_verified(event.target)
            else:
                continue


def test_a_remeasure_round_trips_through_the_eval_client(tmp_path: Path) -> None:
    """The core's remeasure becomes an EvalService.remeasure, charged to the
    Integrator, and its remeasure_ready event becomes the core's measurement."""
    clock = FakeClock()
    blackboard = make_store_blackboard(clock, name="round-trip")
    blackboard.write(HUMAN, {"spec.md": b"# Toy\n"}, "spec")
    eval_service = LadderEvalService(
        blackboard=blackboard,
        sources=(FakeSlowSource(clock=clock, declaration=TOY_SOURCE),),
        indexer=FakeIndexer(),
        budget=RecordingBudget(),
        gates=(),
        gate_runner=passing_gate,
        baseline_scores=zero_baseline_scores,
        arm=PRESETS["S2"],
        params=Params(),
        clock=clock,
        random=Random(0),
    )
    baseline_directory = tmp_path / "baseline0"
    baseline_directory.mkdir()
    (baseline_directory / "policy.py").write_text("SCORE = 0\n", encoding="utf-8")
    task_queue = RecordingTaskQueue()
    integrator = HerdIntegrator(
        blackboard=blackboard,
        eval_service=eval_service,
        task_queue=task_queue,
        compose_queue=RecordingTaskQueue(),
        gates=(),
        gate_runner=ScriptedGates(),
        arm=PRESETS["S2"],
        params=Params(),
        clock=clock,
        top_level=TOY_SOURCE.id,
        work_directory=tmp_path / "integrator",
        baseline_directory=baseline_directory,
        calibration=CoreCalibration(
            per_seed=(0.0,) * 5,
            score=0.0,
            audit_per_seed=None,
            sigma_dev=0.5,
            sigma_audit=0.5,
        ),
    )
    blackboard.subscribe(VerifiedToIntegrator(integrator))
    for author, extra_file in ((WORKER_ONE, "a.txt"), (WORKER_TWO, "b.txt")):
        response = eval_service.request(
            EvalRequest(
                candidate_directory=write_candidate(
                    tmp_path / str(author), author, extra_file
                ),
                predictions={TOY_SOURCE.id: SCORE},
                rationale=f"{author} adds {extra_file}",
            ),
            author,
        )
        assert response.status == "queued"
    for _ in range(4):
        eval_service.tick()
        clock.advance(JUDGE_SECONDS)
        integrator.tick()
    ready = [
        event.data
        for event in integrator.records.events_written
        if isinstance(event.data, IntegrationFailed)
    ]
    assert integrator.current().k == 1, "worker-01 merged; worker-02 was remeasured"
    (submitted,) = integrator.eval_measurer.submitted
    assert submitted.baseline == 1
    ready_events = [
        event_from_json(blackboard.read(path, None).decode())
        for path in blackboard.list_paths("events/eval_service/", None)
    ]
    remeasure_ready = [
        event.data for event in ready_events if isinstance(event.data, RemeasureReady)
    ]
    assert [ready.request_id for ready in remeasure_ready] == [submitted.ticket]
    assert submitted.ticket in integrator.eval_measurer.results
    measurement = integrator.eval_measurer.results[submitted.ticket]
    assert measurement.per_seed == [SCORE] * 5 and measurement.constraint_ok
    assert [failed.reason for failed in ready] == ["below_1se"]
    (follow_up,) = task_queue.tasks
    assert follow_up.kind == "integration_failed_followup"
    assert follow_up.for_agent == WORKER_TWO
    remeasured_manifest = yaml.safe_load(
        blackboard.read(f"candidates/{measurement.hash}/manifest.yaml", None).decode()
    )
    assert isinstance(remeasured_manifest, dict)
    assert remeasured_manifest["author"] == "integrator"
    assert remeasured_manifest["target"] == "baseline@1"


def void_ready(request_id: str, dataset: str) -> RemeasureReady:
    return RemeasureReady.model_validate(
        {
            "request_id": request_id,
            "candidate": "cd" * 32,
            "parent_candidate": "baseline@0",
            "claim_id": "c-eval_service-00009",
            "baseline": "baseline@0",
            "paired": None,
            "score": None,
            "level": "slow",
            "constraint_ok": False,
            "dataset": dataset,
        }
    )


def test_a_void_audit_is_asked_again_and_never_reverts_a_merge(tmp_path: Path) -> None:
    """The judge failed on the closing audit; the core read the empty scores as no
    gain on the audit set and reverted a good merge (first Claude Code run)."""
    harness = make_integrator(tmp_path)
    integrator = harness.integrator
    measurer = integrator.eval_measurer
    candidate = tmp_path / "baseline-copy"
    candidate.mkdir()
    ticket = measurer.submit(
        candidate, 0, "audit", "audit of baseline@0", [], False, 0.0
    )
    integrator.answer(void_ready(ticket, "audit"))
    integrator.answer(void_ready("request-00002", "audit"))
    assert len(harness.eval_service.requests) == 3, "asked again twice"
    assert measurer.result(ticket) is None
    integrator.answer(void_ready("request-00003", "audit"))
    skipped = measurer.result(ticket)
    assert skipped is not None, "after the retries the core gets an answer"
    assert skipped.per_seed == list(integrator.core.versions[0].per_seed)
    assert skipped.constraint_ok is True, "the check passes rather than flags"


def test_a_void_dev_remeasure_still_fails_its_candidate_after_the_retries(
    tmp_path: Path,
) -> None:
    harness = make_integrator(tmp_path)
    integrator = harness.integrator
    candidate = tmp_path / "rebased"
    candidate.mkdir()
    ticket = integrator.eval_measurer.submit(
        candidate, 0, "dev", "remeasure", [PARENT], False, 0.0
    )
    for request_id in (ticket, "request-00002", "request-00003"):
        integrator.answer(void_ready(request_id, "dev"))
    failed = integrator.eval_measurer.result(ticket)
    assert failed is not None and failed.constraint_ok is False
    assert len(harness.eval_service.requests) == 3
