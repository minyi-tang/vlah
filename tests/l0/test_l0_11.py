"""L0-11 (v2.2 §14.1). Owner: W1-09 (#19).

Integrator: textual conflict → issues a compose task; no conflict but the baseline has moved → exactly one paired remeasure; gain not above 1 standard error → integration_failed, and the author gets a follow-up task; under the B1 configuration, workers do not receive baseline_advanced (D74, D58).
"""

from pathlib import Path

import pytest

from tests.fixtures.integrator import WORKER_ONE, WORKER_TWO, make_integrator
from vlah.arms.config import PRESETS, ArmName
from vlah.core.types.events import IntegrationFailed
from vlah.runtime.isolation import worker_view

pytestmark = [pytest.mark.l0, pytest.mark.integrator]

RUN_ID = "r-l0-11"


def test_l0_11_a_conflict_issues_a_compose_task(tmp_path: Path) -> None:
    harness = make_integrator(tmp_path)
    harness.verify(WORKER_ONE, {"params.yaml": "window: 20\nmode: base\n"}, gain=2.0)
    conflicting = harness.verify(
        WORKER_TWO, {"params.yaml": "window: 30\nmode: base\n"}, gain=1.0
    )
    harness.run()
    assert len(harness.events("baseline_advanced")) == 1
    failed = [
        event.data
        for event in harness.events("integration_failed")
        if isinstance(event.data, IntegrationFailed)
    ]
    assert [(item.candidate, item.reason) for item in failed] == [
        (conflicting, "conflict")
    ]
    (compose_task,) = harness.compose_queue.tasks
    assert compose_task.kind == "rebase_compose"
    assert compose_task.pool == "external"
    assert compose_task.refs[0] == conflicting
    assert compose_task.created_by == "integrator"
    assert harness.measurer.submissions == [], "a conflict costs no judge run"


def test_l0_11_a_moved_baseline_costs_exactly_one_paired_remeasure(
    tmp_path: Path,
) -> None:
    harness = make_integrator(tmp_path)
    harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    harness.verify(WORKER_TWO, {"b.txt": "B\n"}, gain=1.0)
    harness.run()
    assert len(harness.events("baseline_advanced")) == 2
    remeasures = [
        submission
        for submission in harness.measurer.submissions
        if submission[0] == "dev"
    ]
    assert len(remeasures) == 1
    assert remeasures[0][1] == 1, "measured on the baseline it was rebased onto"
    assert len(harness.eval_service.requests) == 0, "the scripted measurer stood in"


def test_l0_11_a_gain_within_one_standard_error_is_not_merged(tmp_path: Path) -> None:
    harness = make_integrator(tmp_path)
    flat = harness.verify(WORKER_TWO, {"c.txt": "C\n"}, gain=0.0)
    harness.run()
    assert harness.events("baseline_advanced") == []
    failed = [
        event.data
        for event in harness.events("integration_failed")
        if isinstance(event.data, IntegrationFailed)
    ]
    assert [(item.candidate, item.reason) for item in failed] == [(flat, "below_1se")]
    (follow_up,) = harness.task_queue.tasks
    assert follow_up.kind == "integration_failed_followup"
    assert follow_up.for_agent == WORKER_TWO
    assert follow_up.refs == (harness.claim_by_candidate[flat], flat)
    assert harness.compose_queue.tasks == []


@pytest.mark.parametrize("arm", ["B1", "S2"])
def test_l0_11_only_sharing_arms_see_baseline_advanced(
    tmp_path: Path, arm: ArmName
) -> None:
    """The events are recorded in the run's blackboard in every arm; a B1 worker's
    clone (its mirror) never carries them, nor baseline/ (D58, D67)."""
    harness = make_integrator(tmp_path, arm=PRESETS[arm])
    view = worker_view(harness.blackboard, PRESETS[arm], RUN_ID, WORKER_TWO)
    harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    harness.run()
    assert len(harness.events("baseline_advanced")) == 1
    assert harness.blackboard.list_paths("events/integrator/", None) != ()
    advanced_in_view = [
        path
        for path in view.list_paths("events/integrator/", None)
        if b'"baseline_advanced"' in view.read(path, None)
    ]
    if arm == "B1":
        assert advanced_in_view == []
        assert view.list_paths("baseline/", None) == ()
    else:
        assert len(advanced_in_view) == 1
        assert view.list_paths("baseline/", None) == (
            "baseline/0.json",
            "baseline/1.json",
        )
