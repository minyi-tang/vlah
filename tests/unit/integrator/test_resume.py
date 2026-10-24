"""A restarted Integrator continues from the blackboard (D13, D74, #120)."""

from pathlib import Path

import pytest

from tests.fixtures.integrator import (
    WORKER_ONE,
    WORKER_TWO,
    IntegratorHarness,
    make_integrator,
)
from vlah.core.integrator.resume import events_in_order
from vlah.core.types.events import AuditChecked, BaselineAdvanced
from vlah.core.types.ids import ActorId
from vlah.core.types.params import GateSpec

pytestmark = pytest.mark.integrator

LINT = GateSpec(name="lint", command="lint", when=("guard",))
INTEGRATOR = ActorId("integrator")


def advanced_on_blackboard(harness: IntegratorHarness) -> list[tuple[int, str]]:
    return [
        (event.data.baseline, event.data.candidate)
        for event in events_in_order(harness.blackboard)
        if isinstance(event.data, BaselineAdvanced)
    ]


def test_a_restarted_integrator_merges_onto_the_baseline_it_rebuilt(
    tmp_path: Path,
) -> None:
    """A new process knew only baseline@0, so the second half of a resumed run
    merged onto it and lost every earlier merge (#120)."""
    harness = make_integrator(tmp_path)
    first = harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    second = harness.verify(WORKER_TWO, {"b.txt": "B\n"}, gain=1.0)
    harness.run()
    head_files = harness.files_of(2)
    measured_before = list(harness.measurer.submissions)
    restarted = harness.restart()
    assert harness.measurer.submissions == measured_before, "no judge run to rebuild"
    assert restarted.current().candidates == (first, second)
    assert harness.files_of(2) == head_files
    third = harness.verify(WORKER_ONE, {"c.txt": "C\n"}, gain=1.0, on_baseline=2)
    harness.run()
    current = restarted.current()
    assert (current.k, current.parent, current.candidates) == (
        3,
        2,
        (first, second, third),
    )
    assert advanced_on_blackboard(harness) == [(1, first), (2, second), (3, third)]


def test_spot_checks_continue_from_the_last_one_after_a_restart(
    tmp_path: Path,
) -> None:
    """Replayed merges count towards the next spot check, so without the counters
    from audit_checked a restart checked the segment it had already checked."""
    harness = make_integrator(tmp_path)
    harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    harness.verify(WORKER_TWO, {"b.txt": "B\n"}, gain=1.0)
    harness.run()
    harness.restart()
    for name, gain in (("c.txt", 1.0), ("d.txt", 0.5)):
        harness.verify(WORKER_ONE, {name: f"{name}\n"}, gain, on_baseline=0)
        harness.run()
    segments = [
        event.data.segment
        for event in events_in_order(harness.blackboard)
        if event.actor == INTEGRATOR and isinstance(event.data, AuditChecked)
    ]
    assert segments == [(0, 2), (2, 4)]


def test_claims_left_undecided_or_ended_by_a_stop_are_integrated_after_a_restart(
    tmp_path: Path,
) -> None:
    """The queue lived in memory: a stop failed its candidates with run_ended and a
    crash lost them, though the run went on (#120)."""
    harness = make_integrator(tmp_path)
    merged = harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    harness.run()
    stopped = harness.verify(WORKER_TWO, {"b.txt": "B\n"}, gain=1.0, on_baseline=1)
    harness.integrator.closeout()
    harness.restart()
    crashed = harness.verify(WORKER_ONE, {"c.txt": "C\n"}, gain=1.0, on_baseline=1)
    harness.restart()
    harness.run()
    assert advanced_on_blackboard(harness) == [
        (1, merged),
        (2, stopped),
        (3, crashed),
    ]


def test_a_restart_replays_reverts_and_keeps_the_guard_settled(tmp_path: Path) -> None:
    harness = make_integrator(tmp_path, gates=(LINT,))
    harness.gates.failing_markers.add("BROKEN")
    clean = harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=3.0)
    harness.verify(WORKER_TWO, {"b.txt": "BROKEN one\n"}, gain=2.0)
    harness.verify(WORKER_TWO, {"c.txt": "BROKEN two\n"}, gain=1.0)
    harness.run(ticks=8)
    head_files = harness.files_of(5)
    gate_runs_before = list(harness.gates.runs)
    restarted = harness.restart()
    current = restarted.current()
    assert (current.k, current.candidates, current.guard_pending) == (
        5,
        (clean,),
        False,
    )
    assert harness.files_of(5) == head_files
    assert harness.gates.runs == gate_runs_before, "the recorded gates are reused"
    later = harness.verify(WORKER_ONE, {"d.txt": "D\n"}, gain=1.0, on_baseline=5)
    harness.run()
    assert restarted.current().candidates == (clean, later)
