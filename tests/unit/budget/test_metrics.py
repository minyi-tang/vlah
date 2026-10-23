"""The standing metrics view: prediction error by agent and approach, and the diagnostics (section 11)."""

import pytest

from tests.fixtures.budget import CHEAP_LEVEL, TOP_LEVEL, make_budget
from tests.fixtures.router import make_router
from vlah.arms.config import PRESETS
from vlah.core.types.ids import ActorId

pytestmark = pytest.mark.budget

WORKER = ActorId("worker-01")
OTHER = ActorId("worker-02")


def test_prediction_error_is_averaged_by_agent_and_by_approach() -> None:
    harness = make_budget()
    harness.result("aa01", WORKER, "a-01", TOP_LEVEL, score=0.5, predicted=0.7)
    harness.result("bb02", WORKER, "a-02", TOP_LEVEL, score=0.6, predicted=0.6)
    harness.result("cc03", OTHER, "a-01", CHEAP_LEVEL, score=0.4, predicted=0.1)
    standing = harness.metrics.standing()
    assert standing.prediction_error_by_agent == {
        OTHER: pytest.approx(0.3),
        WORKER: pytest.approx(0.1),
    }
    assert standing.prediction_error_by_approach == {
        "a-01": pytest.approx(0.25),
        "a-02": pytest.approx(0.0),
    }
    assert standing.holdout_score is None, "unknown until the runner scores it"
    assert standing.dead_end_rewalk_count == 0 and standing.insight_reuse_counts == {}


def test_the_holdout_score_and_the_diagnostics_are_recorded() -> None:
    harness = make_budget()
    harness.metrics.record_holdout(0.42)
    harness.meter.record("worker", "a-01", TOP_LEVEL, 1.0)
    harness.metrics.record_best_score(0.5)
    harness.meter.record("worker", "a-01", TOP_LEVEL, 1.0)
    harness.metrics.record_best_score(0.4)
    harness.metrics.record_best_score(0.7)
    harness.metrics.record_rate_limits(WORKER, 3)
    assert harness.metrics.standing().holdout_score == 0.42
    diagnostics = harness.metrics.diagnostics()
    assert diagnostics.best_score_curve == ((1.0, 0.5), (2.0, 0.7))
    assert diagnostics.spend.total_machine_hours == pytest.approx(2.0)
    assert diagnostics.rate_limit_events == 3
    assert diagnostics.interrupts is None, "no router was wired"


def test_the_interrupt_diagnostics_come_from_the_router() -> None:
    harness = make_budget()
    router_harness = make_router(PRESETS["S2"])
    harness.metrics.router = router_harness.router
    diagnostics = harness.metrics.diagnostics()
    assert diagnostics.interrupts is not None
    assert diagnostics.interrupts.latencies_seconds == ()
    assert diagnostics.interrupts.overdue_count == 0
