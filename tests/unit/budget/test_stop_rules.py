"""Stop rules: one escalation at the fraction, one stop at the limit, target reached, per-source runs (D52, C5)."""

import pytest

from tests.fixtures.budget import LIMITS, TOP_LEVEL, make_budget
from vlah.core.stop.rules import EscalationPolicy
from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Limits, Target

pytestmark = pytest.mark.budget

WORKER = ActorId("worker-01")


def test_escalation_is_emitted_once_at_the_fraction() -> None:
    harness = make_budget()
    harness.meter.record("worker", "a-01", TOP_LEVEL, 7.9)
    assert harness.stop_rules.evaluate(10.0) is None
    harness.meter.record("worker", "a-01", TOP_LEVEL, 0.1)
    event = harness.stop_rules.evaluate(11.0)
    assert event is not None and event.type == "escalation"
    assert event.reason is not None
    assert "80%" in event.reason and "freeze_affected_approach" in event.reason
    assert "8 h" in event.reason
    harness.meter.record("worker", "a-01", TOP_LEVEL, 0.5)
    assert harness.stop_rules.evaluate(12.0) is None, "once per run"
    assert len(harness.events_of_type("escalation")) == 1


def test_stop_is_emitted_once_when_the_limit_is_reached() -> None:
    harness = make_budget(policy=EscalationPolicy(total_spend_over_fraction=2.0))
    harness.meter.record("worker", "a-01", TOP_LEVEL, 9.5)
    assert harness.stop_rules.evaluate(1.0) is None
    harness.meter.record("worker", "a-01", TOP_LEVEL, 0.5)
    event = harness.stop_rules.evaluate(2.0)
    assert event is not None and event.type == "stopped"
    assert event.data is not None and event.data.model_dump() == {"reason": "budget"}
    harness.meter.record("worker", "a-01", TOP_LEVEL, 1.0)
    assert harness.stop_rules.evaluate(3.0) is None
    assert len(harness.events_of_type("stopped")) == 1


def test_the_escalation_comes_before_the_stop_when_both_are_due() -> None:
    harness = make_budget()
    harness.meter.record("worker", "a-01", TOP_LEVEL, 10.0)
    first = harness.stop_rules.evaluate(1.0)
    second = harness.stop_rules.evaluate(2.0)
    third = harness.stop_rules.evaluate(3.0)
    assert [event.type for event in (first, second) if event is not None] == [
        "escalation",
        "stopped",
    ]
    assert third is None


def test_per_source_run_counts_are_enforced() -> None:
    harness = make_budget(
        limits=Limits(eval_machine_hours=1000.0, days=1.0, runs={TOP_LEVEL: 3})
    )
    for _ in range(2):
        harness.meter.record("worker", "a-01", TOP_LEVEL, 1.0)
    assert harness.stop_rules.evaluate(1.0) is None
    harness.meter.record("worker", "a-01", "fast", 1.0)
    assert harness.stop_rules.evaluate(2.0) is None, "other sources do not count"
    harness.meter.record("worker", "a-01", TOP_LEVEL, 1.0)
    event = harness.stop_rules.evaluate(3.0)
    assert event is not None and event.type == "stopped"
    assert event.reason is not None and "3 runs" in event.reason


def test_target_reached_escalates_and_does_not_stop() -> None:
    target = Target(source=TOP_LEVEL, score=0.9, direction="higher")
    harness = make_budget(target=target)
    harness.result("aa01", WORKER, "a-01", TOP_LEVEL, score=0.85, predicted=0.8)
    harness.advance_baseline("aa01")
    assert harness.stop_rules.evaluate(1.0) is None
    harness.result("bb02", WORKER, "a-01", TOP_LEVEL, score=0.95, predicted=0.9)
    harness.advance_baseline("bb02")
    event = harness.stop_rules.evaluate(2.0)
    assert (
        event is not None
        and event.type == "escalation"
        and event.target == "target_reached"
    )
    assert event.evidence == ("charter/progress.md", "baseline/2.json")
    assert harness.stop_rules.evaluate(3.0) is None, "once, and never a stop"
    assert harness.events_of_type("stopped") == []


def test_a_lower_is_better_target_counts_downwards() -> None:
    target = Target(source=TOP_LEVEL, score=100.0, direction="lower")
    harness = make_budget(target=target)
    harness.result("aa01", WORKER, "a-01", TOP_LEVEL, score=120.0, predicted=110.0)
    harness.advance_baseline("aa01")
    assert harness.stop_rules.evaluate(1.0) is None
    harness.result("bb02", WORKER, "a-01", TOP_LEVEL, score=99.0, predicted=100.0)
    harness.advance_baseline("bb02")
    event = harness.stop_rules.evaluate(2.0)
    assert event is not None and event.target == "target_reached"


def test_a_rule_fired_before_a_restart_stays_fired() -> None:
    harness = make_budget()
    harness.meter.record("worker", "a-01", TOP_LEVEL, LIMITS.eval_machine_hours)
    assert harness.stop_rules.evaluate(1.0) is not None
    assert harness.stop_rules.evaluate(2.0) is not None
    restarted = make_budget()
    restarted.stop_rules.store = harness.store
    restarted.stop_rules.meter = harness.meter
    assert restarted.stop_rules.evaluate(3.0) is None, "the key-value store remembers"


def test_a_failed_write_leaves_the_rule_due_for_the_next_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supervisor swallows a raising evaluate; the stop must not be lost with it."""
    harness = make_budget(policy=EscalationPolicy(total_spend_over_fraction=2.0))
    harness.meter.record("worker", "a-01", TOP_LEVEL, LIMITS.eval_machine_hours)
    original_write = harness.stop_rules.write
    attempts: list[str] = []

    def write_once_failing(
        event_type: str, now_seconds: float, **fields: object
    ) -> Event:
        attempts.append(event_type)
        if len(attempts) == 1:
            raise ConnectionError("the git store is locked")
        else:
            return original_write(event_type, now_seconds, **fields)

    monkeypatch.setattr(harness.stop_rules, "write", write_once_failing)
    with pytest.raises(ConnectionError):
        harness.stop_rules.evaluate(1.0)
    event = harness.stop_rules.evaluate(2.0)
    assert event is not None and event.type == "stopped"
    assert attempts == ["stopped", "stopped"]
    assert harness.stop_rules.evaluate(3.0) is None, "and then once, as before"
