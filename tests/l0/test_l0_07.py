"""L0-07 (v2.2 §14.1). Owner: W1-04 (#14).

gate fails → not counted in the score; writes dead_end reason=gate:<name> (D54).
"""

from pathlib import Path

import pytest

from tests.fixtures.eval_harness import WORKER, make_eval_harness
from vlah.core.types.params import GateSpec

pytestmark = [pytest.mark.l0, pytest.mark.eval]


def test_l0_07_gate_failure_records_dead_end(tmp_path: Path) -> None:
    """gate fails → not counted in the score; writes dead_end reason=gate:<name> (D54)."""
    harness = make_eval_harness(
        tmp_path,
        gates=(GateSpec(name="unit_tests", command="pytest", when=("pre_submit",)),),
    )
    harness.gates.failing.add("unit_tests")
    response = harness.service.request(harness.request(harness.candidate(9.0)), WORKER)
    harness.run(seconds=3600)
    assert response.status == "gate_failed"
    assert not harness.sources[0].jobs, "a failed gate never reaches the judge"
    assert not any(
        event.type == "result_ready" for event in harness.events_on_blackboard()
    )
    dead_ends = [
        harness.blackboard.read(path, None)
        for path in harness.blackboard.list_paths("claims/eval_service/", None)
    ]
    assert any(
        b"type: dead_end" in claim and b"gate:unit_tests" in claim
        for claim in dead_ends
    )
