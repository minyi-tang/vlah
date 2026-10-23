"""L0-09 (v2.2 §14.1). Owner: W1-10 (#20).

Budget meter accumulates to the cap → stop event (D52).
"""

import pytest

from tests.fixtures.budget import TOP_LEVEL, make_budget
from vlah.core.stop.rules import EscalationPolicy
from vlah.core.types.params import Limits

pytestmark = [pytest.mark.l0, pytest.mark.budget]

RUN_COST_HOURS = 1.0
# The machine-hour cap alone; per-source run counts are another rule.
LIMITS = Limits(eval_machine_hours=10.0, days=1.0)


def test_l0_09_budget_cap_stops() -> None:
    """Budget meter accumulates to the cap → stop event (D52)."""
    harness = make_budget(
        limits=LIMITS, policy=EscalationPolicy(total_spend_over_fraction=2.0)
    )
    runs_to_cap = int(LIMITS.eval_machine_hours / RUN_COST_HOURS)
    for number in range(1, runs_to_cap + 1):
        harness.meter.record("worker", "a-01", TOP_LEVEL, RUN_COST_HOURS)
        event = harness.stop_rules.evaluate(float(number))
        if number < runs_to_cap:
            assert event is None, f"no stop at {number} of {runs_to_cap} runs"
        else:
            assert event is not None and event.type == "stopped"
            assert event.data is not None and event.data.model_dump() == {
                "reason": "budget"
            }
    (stopped,) = harness.events_of_type("stopped")
    assert stopped["actor"] == "ci", "the stop is on the blackboard for the closeout"
    assert harness.meter.spent().fraction_of_limit == pytest.approx(1.0)
