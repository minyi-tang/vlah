"""L0-15 (v2.2 §14.1). Owner: W1-09 (#19).

A guard batch fails → bisect, write a new baseline version that excludes the culprit, and leave old records unchanged; the deliverable is never guard_pending (D74).
"""

from pathlib import Path

import pytest

from tests.fixtures.integrator import WORKER_ONE, WORKER_TWO, make_integrator
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.events import BaselineReverted
from vlah.core.types.params import GateSpec

pytestmark = [pytest.mark.l0, pytest.mark.integrator]

LINT = GateSpec(name="lint", command="lint", when=("guard",))


def test_l0_15_guard_batch_bisects(tmp_path: Path) -> None:
    harness = make_integrator(tmp_path, gates=(LINT,))
    harness.gates.failing_markers.add("BROKEN")
    first = harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=3.0)
    culprit = harness.verify(WORKER_TWO, {"b.txt": "BROKEN\n"}, gain=2.0)
    third = harness.verify(WORKER_ONE, {"c.txt": "C\n"}, gain=1.0)
    harness.run(ticks=1)
    old_records = {
        path: harness.blackboard.read(path, None)
        for path in harness.blackboard.list_paths("baseline/", None)
    }
    assert sorted(old_records) == [f"baseline/{k}.json" for k in range(3)]
    assert harness.events("baseline_reverted") == [], "the batch is not due yet"
    harness.run(ticks=5)
    (revert,) = [
        event.data
        for event in harness.events("baseline_reverted")
        if isinstance(event.data, BaselineReverted)
    ]
    assert (revert.from_baseline, revert.new_baseline, revert.removed) == (
        3,
        4,
        (culprit,),
    )
    assert harness.gates.runs[:3] == [("lint", "3"), ("lint", "1"), ("lint", "2")]
    for path, before in old_records.items():
        assert harness.blackboard.read(path, None) == before, f"{path} was rewritten"
    superseded = BaselineRecord.model_validate_json(
        harness.blackboard.read("baseline/3.json", None)
    )
    assert superseded.candidates == (first, culprit, third), "still as it was merged"
    assert superseded.guard_pending is True
    new_record = BaselineRecord.model_validate_json(
        harness.blackboard.read("baseline/4.json", None)
    )
    assert new_record.candidates == (first, third)
    assert new_record.removed == (culprit,)
    assert new_record.guard_pending is False
    assert new_record.gates == {"lint": True}
    deliverable = harness.integrator.closeout()
    assert deliverable.k == 4
    assert deliverable.guard_pending is False
    assert culprit not in deliverable.candidates
    quarantined = [
        event.target
        for event in harness.events("claim_status")
        if event.to == "quarantined"
    ]
    assert quarantined == [harness.claim_by_candidate[culprit]]
