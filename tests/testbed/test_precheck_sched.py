"""Spec 11.6 precheck (day 3): slow (~10-20 min). Run with:  pytest -m slow tests/test_precheck_sched.py -s"""

from typing import Any

import pytest

from testbed.problem_api import load_problem


@pytest.mark.slow
@pytest.mark.timeout(7200)
def test_precheck_sched():
    problem: Any = load_problem("sched")
    if problem.hidden is None:
        pytest.skip("hidden material (private/testbed/sched) is not here")
    precheck = problem.hidden_module("reference.precheck")
    report = precheck.run(problem, n_seeds=20, probes=True)
    precheck.print_report(report)
    assert report["ok"], {
        name: (check["value"], check["target"])
        for name, check in report["checks"].items()
        if not check["ok"]
    }
    assert report["probes"]["reference"] == [
        "admission",
        "backfill",
        "est_order",
        "locality",
        "packing",
    ]
    assert report["probes"]["baseline"] == []
