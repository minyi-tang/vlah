"""Spec 11.6 precheck (day 3): slow (~10-20 min). Run with:  pytest -m slow tests/test_precheck_sched.py -s"""

from typing import Any

import pytest

from testbed.problem_api import load_problem


@pytest.mark.slow
@pytest.mark.timeout(7200)
def test_precheck_sched():
    p: Any = load_problem("sched")
    if p.hidden is None:
        pytest.skip("hidden material (private/testbed/sched) is not here")
    mod = p.hidden_module("reference.precheck")
    rep = mod.run(p, n_seeds=20, probes=True)
    mod.print_report(rep)
    assert rep["ok"], {
        k: (c["value"], c["target"]) for k, c in rep["checks"].items() if not c["ok"]
    }
    assert rep["probes"]["reference"] == [
        "admission",
        "backfill",
        "est_order",
        "locality",
        "packing",
    ]
    assert rep["probes"]["baseline"] == []
