"""Remeasures on the audit set: the dataset reaches the judge and the records (D72, #95)."""

import json
from pathlib import Path
from typing import Literal

import pytest

from tests.fixtures.eval_harness import SEED_COUNT, make_eval_harness
from vlah.adapters.memory.signal_source import FAKE_SCORE_FILE_NAME
from vlah.core.eval.api import RemeasureRequest
from vlah.core.types.events import RemeasureReady
from vlah.core.types.params import Params
from vlah.ports.signal_source import Dataset

pytestmark = pytest.mark.eval

DAILY_SCORE = 3.0
AUDIT_SCORE = 1.0
DAY_SECONDS = 8 * 3600


def dev_only_baseline(
    source_id: str, baseline: str, dataset: Dataset
) -> tuple[float, ...] | None:
    """A runtime that knows the baseline's daily scores but not its audit scores."""
    return None if dataset == "audit" else (0.0,) * SEED_COUNT


@pytest.mark.parametrize(
    ("dataset", "expected_score"),
    [("dev", DAILY_SCORE), ("audit", AUDIT_SCORE)],
    ids=["dev", "audit"],
)
def test_a_remeasure_runs_on_the_requested_dataset(
    tmp_path: Path, dataset: Literal["dev", "audit"], expected_score: float
) -> None:
    harness = make_eval_harness(tmp_path)
    directory = harness.candidate(DAILY_SCORE, name=f"measured-{dataset}")
    (directory / FAKE_SCORE_FILE_NAME).write_text(
        json.dumps(
            {
                "per_seed": [DAILY_SCORE] * SEED_COUNT,
                "audit_per_seed": [AUDIT_SCORE] * SEED_COUNT,
            }
        ),
        encoding="utf-8",
    )
    request_id = harness.service.remeasure(
        RemeasureRequest(
            candidate_directory=directory,
            baseline="baseline@0",
            parent_candidate="baseline@0",
            approach="a-01",
            dataset=dataset,
        )
    )
    events = harness.run(seconds=DAY_SECONDS)
    (ready,) = [
        event.data for event in events if isinstance(event.data, RemeasureReady)
    ]
    assert ready.request_id == request_id
    assert ready.score == expected_score
    assert ready.dataset == dataset
    assert ready.per_seed == (expected_score,) * SEED_COUNT
    assert ready.paired is not None and ready.paired.mean == expected_score
    judge_jobs = [job for source in harness.sources for job in source.jobs.values()]
    assert [job.dataset for job in judge_jobs] == [dataset]
    result = json.loads(
        harness.blackboard.read(f"candidates/{ready.candidate}/result.json", None)
    )
    assert [level["dataset"] for level in result["levels"]] == [dataset]


def test_an_audit_remeasure_is_not_held_for_unknown_baseline_audit_scores(
    tmp_path: Path,
) -> None:
    harness = make_eval_harness(tmp_path, baseline_scores=dev_only_baseline)
    directory = harness.candidate(DAILY_SCORE, name="audited")
    harness.service.remeasure(
        RemeasureRequest(
            candidate_directory=directory,
            baseline="baseline@3",
            parent_candidate="baseline@3",
            approach="a-01",
            dataset="audit",
        )
    )
    events = harness.run(seconds=DAY_SECONDS)
    (ready,) = [
        event.data for event in events if isinstance(event.data, RemeasureReady)
    ]
    assert ready.score == DAILY_SCORE, "no audit scores in the fake: the daily ones"
    assert ready.paired is None
    assert ready.constraint_ok is True
    verified = [
        event
        for event in events
        if event.type == "claim_status" and event.to is not None
    ]
    assert [event.to for event in verified] == ["verified:slow"]


def audit_request(directory: Path, baseline: str = "baseline@0") -> RemeasureRequest:
    return RemeasureRequest(
        candidate_directory=directory,
        baseline=baseline,
        parent_candidate=baseline,
        approach="a-01",
        dataset="audit",
    )


def test_an_audit_measurement_does_not_wait_for_the_integrator_hourly_slot(
    tmp_path: Path,
) -> None:
    """A spot check's measurement has its own cap (D72); sharing the one remeasure
    per hour would stall integration for an hour at every check."""
    harness = make_eval_harness(tmp_path)
    harness.service.remeasure(
        RemeasureRequest(
            candidate_directory=harness.candidate(DAILY_SCORE, name="rebased"),
            baseline="baseline@0",
            parent_candidate="baseline@0",
            approach="a-01",
        )
    )
    audited = harness.candidate(DAILY_SCORE, name="v1")
    (audited / FAKE_SCORE_FILE_NAME).write_text(
        json.dumps(
            {
                "per_seed": [DAILY_SCORE] * SEED_COUNT,
                "audit_per_seed": [AUDIT_SCORE] * SEED_COUNT,
            }
        ),
        encoding="utf-8",
    )
    harness.service.remeasure(audit_request(audited))
    events = harness.run(seconds=1800)
    ready = [event.data for event in events if isinstance(event.data, RemeasureReady)]
    assert sorted(item.score or 0.0 for item in ready) == [AUDIT_SCORE, DAILY_SCORE]
    assert sorted(item.dataset for item in ready) == ["audit", "dev"]


def test_audit_measurements_are_capped_per_run(tmp_path: Path) -> None:
    harness = make_eval_harness(tmp_path, params=Params(holdout_run_cap=1))
    for name in ("v1", "v2"):
        harness.service.remeasure(
            audit_request(harness.candidate(DAILY_SCORE, name=name))
        )
    events = harness.run(seconds=DAY_SECONDS)
    ready = [event.data for event in events if isinstance(event.data, RemeasureReady)]
    assert len(ready) == 1, "the second waits for a cap that never frees"
