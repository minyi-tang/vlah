"""L0-20 (v2.2 §14.1). Owner: W1-09 (#19).

Closeout order: budget exhausted → stop accepting → guard_pending catch-up check → final spot check → freeze the deliverable; the deliverable is not guard_pending (D74, D79).
"""

from pathlib import Path

import pytest

from tests.fixtures.integrator import WORKER_ONE, WORKER_TWO, make_integrator
from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.core.budget.meter import KeyValueBudgetMeter
from vlah.core.stop.rules import BudgetStopRules, EscalationPolicy
from vlah.core.types.events import AuditChecked, Stopped
from vlah.core.types.params import GateSpec, Limits, Params

pytestmark = [pytest.mark.l0, pytest.mark.integrator]

LINT = GateSpec(name="lint", command="lint", when=("guard",))
LIMITS = Limits(eval_machine_hours=1.0, days=1.0)


def test_l0_20_closeout_order(tmp_path: Path) -> None:
    # No periodic spot check before the closeout, so the final one is the first.
    harness = make_integrator(
        tmp_path, params=Params(spot_check_every_merges=5), gates=(LINT,)
    )
    harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    harness.verify(WORKER_TWO, {"b.txt": "B\n"}, gain=1.0)
    harness.run()
    assert harness.integrator.current().guard_pending is True, "two merges, batch of 3"
    assert harness.events("audit_checked") == []

    # Budget exhausted: the stop rules fire stopped(reason="budget") (D52).
    store = MemoryKeyValueStore(clock=harness.clock, namespace="l0-20")
    meter = KeyValueBudgetMeter(store=store, limits=LIMITS)
    stop_rules = BudgetStopRules(
        store=store,
        blackboard=harness.blackboard,
        meter=meter,
        integrator=harness.integrator,
        limits=LIMITS,
        target=None,
        policy=EscalationPolicy(),
    )
    meter.record("worker", "a-01", "slow", LIMITS.eval_machine_hours)
    fired = [stop_rules.evaluate(harness.clock.now_seconds()) for _ in range(2)]
    stopped = [
        event for event in fired if event is not None and event.type == "stopped"
    ]
    assert len(stopped) == 1 and isinstance(stopped[0].data, Stopped)
    assert stopped[0].data.reason == "budget"

    # Stop accepting → guard catch-up → final spot check → freeze.
    journal_start = len(harness.measurer.journal)
    deliverable = harness.integrator.closeout()
    late = harness.verify(WORKER_ONE, {"c.txt": "C\n"}, gain=1.0, on_baseline=2)
    harness.run()
    assert harness.measurer.journal[journal_start:] == [
        "gate lint on baseline@2",
        "audit measurement of baseline@2",
    ]
    final_audits = [
        event.data
        for event in harness.events("audit_checked")
        if isinstance(event.data, AuditChecked)
    ]
    assert [audit.flagged for audit in final_audits] == [False]
    assert deliverable.k == 2
    assert deliverable.guard_pending is False
    assert deliverable.gates == {"lint": True}
    assert harness.integrator.closeout() == deliverable, "frozen"
    assert late not in harness.integrator.current().candidates
    assert len(harness.events("baseline_advanced")) == 2
