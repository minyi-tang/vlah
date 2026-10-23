"""L0-16 (v2.2 §14.1). Owner: W1-09 (#19).

Spot check: a scripted overfitting candidate is flagged, reverted, and set to quarantined; in a clean scripted run, none of the 7 spot checks raises an alarm (D72).
"""

from pathlib import Path

import pytest

from tests.fixtures.integrator import WORKER_ONE, WORKER_TWO, make_integrator
from vlah.core.types.events import AuditChecked, BaselineReverted
from vlah.core.types.params import Params

pytestmark = [pytest.mark.l0, pytest.mark.integrator]

CLEAN_MERGES = 14


def audits(harness_events: list[AuditChecked]) -> list[bool]:
    return [audit.flagged for audit in harness_events]


def test_l0_16_an_overfitting_candidate_is_flagged_reverted_and_quarantined(
    tmp_path: Path,
) -> None:
    harness = make_integrator(tmp_path)
    honest = harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    harness.run()
    overfit = harness.verify(
        WORKER_TWO, {"b.txt": "B\n"}, gain=3.0, audit_gain=0.0, on_baseline=1
    )
    harness.run()
    checked = [
        event.data
        for event in harness.events("audit_checked")
        if isinstance(event.data, AuditChecked)
    ]
    assert audits(checked) == [True]
    assert checked[0].segment == (0, 2)
    (revert,) = [
        event.data
        for event in harness.events("baseline_reverted")
        if isinstance(event.data, BaselineReverted)
    ]
    assert (revert.new_baseline, revert.removed, revert.reason) == (
        3,
        (overfit,),
        "audit",
    )
    quarantined = [
        event for event in harness.events("claim_status") if event.to == "quarantined"
    ]
    assert [event.target for event in quarantined] == [
        harness.claim_by_candidate[overfit]
    ]
    assert quarantined[0].from_ == "verified:slow"
    current = harness.integrator.current()
    assert current.candidates == (honest,)
    assert current.per_seed == tuple([12.0] * 5), "back to the honest baseline's scores"


def test_l0_16_a_clean_run_makes_seven_spot_checks_without_an_alarm(
    tmp_path: Path,
) -> None:
    harness = make_integrator(tmp_path)
    assert Params().holdout_run_cap - Params().spot_check_reserved_for_bisection == 7
    for number in range(1, CLEAN_MERGES + 1):
        harness.verify(
            WORKER_ONE,
            {f"f{number}.txt": f"F{number}\n"},
            gain=1.0,
            on_baseline=harness.integrator.core.k,
        )
        harness.run(ticks=3)
    assert len(harness.events("baseline_advanced")) == CLEAN_MERGES
    checked = [
        event.data
        for event in harness.events("audit_checked")
        if isinstance(event.data, AuditChecked)
    ]
    assert audits(checked) == [False] * 7
    assert harness.events("baseline_reverted") == []
    audit_measurements = [
        submission
        for submission in harness.measurer.submissions
        if submission[0] == "audit"
    ]
    assert len(audit_measurements) == 7, "one fresh audit measurement per check"
