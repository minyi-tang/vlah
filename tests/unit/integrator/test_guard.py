"""Guard batches beyond L0-15: pending state, and a batch with two culprits (D74)."""

from pathlib import Path

import pytest

from tests.fixtures.integrator import WORKER_ONE, WORKER_TWO, make_integrator
from vlah.core.types.events import BaselineReverted, IntegrationFailed
from vlah.core.types.params import GateSpec

pytestmark = pytest.mark.integrator

LINT = GateSpec(name="lint", command="lint", when=("guard",))


def test_merges_are_guard_pending_until_their_batch_passes(tmp_path: Path) -> None:
    harness = make_integrator(tmp_path, gates=(LINT,))
    harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=3.0)
    harness.run()
    assert harness.integrator.current().guard_pending is True
    assert b'"guard_pending": true' in harness.blackboard.read("baseline/1.json", None)
    harness.verify(WORKER_ONE, {"b.txt": "B\n"}, gain=2.0)
    harness.verify(WORKER_TWO, {"c.txt": "C\n"}, gain=1.0)
    harness.run()
    current = harness.integrator.current()
    assert (current.k, current.guard_pending, current.gates) == (
        3,
        False,
        {"lint": True},
    )
    assert harness.gates.runs == [("lint", "3")], "one batch, on the head only"


def test_a_second_culprit_is_reverted_one_merge_at_a_time(tmp_path: Path) -> None:
    harness = make_integrator(tmp_path, gates=(LINT,))
    harness.gates.failing_markers.add("BROKEN")
    clean = harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=3.0)
    first = harness.verify(WORKER_TWO, {"b.txt": "BROKEN one\n"}, gain=2.0)
    second = harness.verify(WORKER_TWO, {"c.txt": "BROKEN two\n"}, gain=1.0)
    harness.run(ticks=8)
    reverts = [
        event.data
        for event in harness.events("baseline_reverted")
        if isinstance(event.data, BaselineReverted)
    ]
    assert [(revert.new_baseline, revert.removed) for revert in reverts] == [
        (4, (first,)),
        (5, (second,)),
    ]
    current = harness.integrator.current()
    assert (current.k, current.candidates, current.guard_pending) == (
        5,
        (clean,),
        False,
    )
    quarantined = [
        event.target
        for event in harness.events("claim_status")
        if event.to == "quarantined"
    ]
    assert quarantined == [
        harness.claim_by_candidate[first],
        harness.claim_by_candidate[second],
    ]
    assert harness.blackboard.list_paths("baseline/5.json", None) == (
        "baseline/5.json",
    )


def test_closeout_stops_accepting_before_the_guard_catch_up(tmp_path: Path) -> None:
    """D79 order: a candidate verified before closeout but not yet integrated fails
    with run_ended, even when the guard catch-up reverts and steps the core."""
    harness = make_integrator(tmp_path, gates=(LINT,))
    harness.gates.failing_markers.add("BROKEN")
    clean = harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=3.0)
    culprit = harness.verify(WORKER_TWO, {"b.txt": "BROKEN\n"}, gain=2.0)
    harness.run()
    assert harness.integrator.current().k == 2
    late = harness.verify(WORKER_ONE, {"c.txt": "C\n"}, gain=1.0, on_baseline=2)
    deliverable = harness.integrator.closeout()
    assert (deliverable.k, deliverable.candidates) == (3, (clean,))
    assert deliverable.removed == (culprit,)
    failed = [
        event.data
        for event in harness.events("integration_failed")
        if isinstance(event.data, IntegrationFailed)
    ]
    assert [(item.candidate, item.reason) for item in failed] == [(late, "run_ended")]
    assert harness.task_queue.tasks == [], "no follow-up once the run has ended"
