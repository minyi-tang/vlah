"""The budget meter: breakdowns sum to the total, concurrent writers add up, tokens never cap (D43, P10)."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.fixtures.budget import CHEAP_LEVEL, TOP_LEVEL, make_budget

pytestmark = pytest.mark.budget


def test_the_breakdowns_sum_to_the_total() -> None:
    harness = make_budget()
    harness.meter.record("worker", "a-01", TOP_LEVEL, 1.0)
    harness.meter.record("worker", "a-02", CHEAP_LEVEL, 0.25)
    harness.meter.record("integrator", None, TOP_LEVEL, 1.0)
    view = harness.meter.spent()
    assert view.total_machine_hours == pytest.approx(2.25)
    for breakdown in (
        view.machine_hours_by_role,
        view.machine_hours_by_approach,
        view.machine_hours_by_source,
    ):
        assert sum(breakdown.values()) == pytest.approx(view.total_machine_hours)
    assert view.machine_hours_by_role == {"integrator": 1.0, "worker": 1.25}
    assert view.machine_hours_by_approach == {"(none)": 1.0, "a-01": 1.0, "a-02": 0.25}
    assert view.fraction_of_limit == pytest.approx(0.225)
    assert harness.meter.counts().runs_by_source == {CHEAP_LEVEL: 1, TOP_LEVEL: 2}


def test_concurrent_writers_add_up_exactly() -> None:
    harness = make_budget()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda number: harness.meter.record(
                    "worker", f"a-{number % 3:02d}", CHEAP_LEVEL, 0.01
                ),
                range(400),
            )
        )
    view = harness.meter.spent()
    assert view.total_machine_hours == pytest.approx(4.0)
    assert sum(view.machine_hours_by_approach.values()) == pytest.approx(4.0)
    assert harness.meter.counts().runs_by_source == {CHEAP_LEVEL: 400}


def test_tokens_are_recorded_and_never_cap_anything() -> None:
    harness = make_budget()
    harness.meter.record_tokens("worker", 50_000_000)
    assert harness.meter.counts().tokens_by_role == {"worker": 50_000_000}
    assert harness.meter.spent().total_machine_hours == 0.0
    assert harness.stop_rules.evaluate(0.0) is None, "tokens never stop a run (P10)"
