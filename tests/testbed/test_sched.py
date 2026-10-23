"""Spec 11.9 tests for problem A (the scheduling simulator), plus sandbox and packing checks."""

import importlib
import json
import sys
import time
from typing import Any

import pytest

from testbed.cli.pack import leaked, pack_problem
from testbed.problem_api import load_problem

PROBLEM: Any = load_problem(
    "sched"
)  # SchedProblem; loaded by path, so typed as the generic Problem
INGREDIENTS = {
    "backfill",
    "admission",
    "locality",
    "packing",
    "est_order",
    "trap_sjf",
    "fairshare",
}
needs_hidden = pytest.mark.skipif(
    PROBLEM.hidden is None, reason="hidden material (private/testbed/sched) is not here"
)


def write_policy(tmp_path, code, name="cand"):
    candidate_dir = tmp_path / name
    candidate_dir.mkdir()
    (candidate_dir / "policy.py").write_text(code)
    (candidate_dir / "params.yaml").write_text("{}\n")
    return candidate_dir


def short_problem(hours=2):
    problem = type(PROBLEM)(PROBLEM.root)
    problem.common = dict(problem.common, horizon_s=hours * 3600, drain_cap_s=3600)
    return problem


@pytest.mark.timeout(300)
def test_pack_excludes_hidden_material(tmp_path):
    packed_dir = pack_problem("sched", tmp_path / "packed")
    assert leaked(packed_dir) == []
    for must in (
        "sim/engine.py",
        "sim/policy_api.py",
        "sites/site1.yaml",
        "policy/policy.py",
        "map/gap.py",
        "baseline_scores.json",
        "problem.py",
    ):
        assert (packed_dir / must).exists(), must
    for gone in ("hidden", "reference", "probes", "problem.yaml", "pi_prompt.md"):
        assert not (packed_dir / gone).exists(), gone
    if PROBLEM.hidden is not None:
        secret = str(PROBLEM.seeds("dev")[0])
        for packed_file in packed_dir.rglob("*"):
            if packed_file.is_file():
                assert secret not in packed_file.read_text(errors="ignore"), packed_file
    packed = load_problem(packed_dir)  # the worker copy works without hidden files...
    assert packed.public_seeds == [1, 2, 3, 4, 5]
    with pytest.raises(PermissionError):
        packed.seeds("dev")  # ...but has no hidden seeds
    if PROBLEM.hidden is None:
        with pytest.raises(PermissionError):
            packed.probes(PROBLEM.baseline_dir())


def test_fast_tier_only_public_seeds():
    with pytest.raises(PermissionError):
        PROBLEM.evaluate(PROBLEM.baseline_dir(), "fast", 123456789)


@pytest.mark.timeout(300)
def test_same_seed_is_byte_identical_without_timeouts(tmp_path):
    candidate_dir = write_policy(
        tmp_path,
        "def order(queue, state):\n    return [j.id for j in sorted(queue, key=lambda j: (j.est_dur, j.id))]\n",
    )
    first = PROBLEM.evaluate_many(candidate_dir, [3], "fast", timeout=False)[0]
    second = PROBLEM.evaluate_many(candidate_dir, [3], "fast", timeout=False)[0]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


@pytest.mark.timeout(300)
def test_one_seed_six_sites_under_60s():
    started_at = time.time()
    result = PROBLEM.evaluate(PROBLEM.baseline_dir(), "fast", 2)
    assert result["score"] is not None and time.time() - started_at < 60


@needs_hidden
@pytest.mark.timeout(300)
def test_probes_reference_and_baseline():
    reference = {
        ingredient
        for ingredient, fired in PROBLEM.probes(PROBLEM.reference_dir()).items()
        if fired
    }
    assert reference == {"backfill", "admission", "locality", "packing", "est_order"}
    assert not any(PROBLEM.probes(PROBLEM.baseline_dir()).values())
    assert set(PROBLEM.probes(PROBLEM.baseline_dir())) == INGREDIENTS


def test_slow_hooks_over_one_percent_void_the_run(tmp_path):
    candidate_dir = write_policy(
        tmp_path,
        """
import time
_n = [0]
def order(queue, state):
    _n[0] += 1
    if _n[0] % 10 == 0:
        t = time.process_time()
        while time.process_time() - t < 0.06:
            pass
    return [j.id for j in sorted(queue, key=lambda j: (j.enqueue_t, j.id))]
""",
    )
    problem = short_problem()
    result = problem.assemble_sites(
        [problem.site_result(str(candidate_dir), 1, i) for i in (0, 4)]
    )
    assert result["score"] is None and result["extra"]["timed_out"] is True


def test_invalid_answers_count_as_timeouts(tmp_path):
    candidate_dir = write_policy(tmp_path, "def order(queue, state):\n    return []\n")
    problem = short_problem()
    result = problem.assemble_sites([problem.site_result(str(candidate_dir), 1, 4)])
    assert result["score"] is None and result["extra"]["timeout_ratio"] > 0.01


def test_crashing_import_voids(tmp_path):
    candidate_dir = write_policy(tmp_path, "raise RuntimeError('boom')\n")
    problem = short_problem()
    result = problem.assemble_sites([problem.site_result(str(candidate_dir), 1, 4)])
    assert result["score"] is None and "boom" in result["extra"]["reason"]


def test_policy_sandbox_blocks_network_and_hidden_files(tmp_path):
    hidden = (PROBLEM.hidden or PROBLEM.root) / "problem.yaml"
    candidate_dir = write_policy(
        tmp_path,
        f"""
import socket
try:
    socket.create_connection(("127.0.0.1", 9), timeout=0.2); NET = "open"
except Exception as e:
    NET = type(e).__name__
try:
    open({str(hidden)!r}).read(); READ = "open"
except Exception as e:
    READ = type(e).__name__
def order(queue, state):
    raise RuntimeError(f"net={{NET}} read={{READ}}")
""",
    )
    engine = importlib.import_module(
        type(PROBLEM).__module__.rsplit(".", 1)[0] + ".sim.engine"
    )
    driver = engine.PolicyDriver(
        candidate_dir, 5, PROBLEM.sites[4], timeout=True, budget_s=0.05
    )
    try:
        reply = driver.request(
            {
                "op": "call",
                "hook": "order",
                "now": 0.0,
                "machines": [],
                "running": [],
                "reset_queue": [],
            }
        )
    finally:
        driver.close()
    message = " ".join(reply["errors"])
    assert "net=open" not in message
    if sys.platform == "darwin":
        assert "read=open" not in message
