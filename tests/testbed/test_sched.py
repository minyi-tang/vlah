"""Spec 11.9 tests for problem A (the scheduling simulator), plus sandbox and packing checks."""

import json
import sys
import time
from typing import Any

import pytest
import yaml

from testbed.cli.pack import leaked, pack_problem
from testbed.problem_api import load_problem

P: Any = load_problem(
    "sched"
)  # SchedProblem; loaded by path, so typed as the generic Problem
INGS = {
    "backfill",
    "admission",
    "locality",
    "packing",
    "est_order",
    "trap_sjf",
    "fairshare",
}
needs_hidden = pytest.mark.skipif(
    P.hidden is None, reason="hidden material (private/testbed/sched) is not here"
)


def write_policy(tmp_path, code, name="cand"):
    d = tmp_path / name
    d.mkdir()
    (d / "policy.py").write_text(code)
    (d / "params.yaml").write_text("{}\n")
    return d


def short_problem(hours=2):
    """The public tier with a short trace, for tests that only need a few scheduling passes."""
    q = type(P)(P.root)
    q.public_common = dict(
        q.public_common,
        horizon_s=hours * 3600,
        fast_horizon_s=hours * 3600,
        drain_cap_s=3600,
    )
    return q


@pytest.mark.timeout(300)
def test_pack_excludes_hidden_material(tmp_path):
    out = pack_problem("sched", tmp_path / "packed")
    assert leaked(out) == []
    for must in (
        "sim/engine.py",
        "sim/policy_api.py",
        "sites/site1.yaml",
        "policy/policy.py",
        "map/gap.py",
        "baseline_scores.json",
        "problem.py",
    ):
        assert (out / must).exists(), must
    for gone in ("hidden", "reference", "probes", "problem.yaml", "pi_prompt.md"):
        assert not (out / gone).exists(), gone
    if P.hidden is not None:
        secret = str(P.seeds("dev")[0])
        for f in out.rglob("*"):
            if f.is_file():
                assert secret not in f.read_text(errors="ignore"), f
    packed = load_problem(out)  # the worker copy works without hidden files...
    assert packed.public_seeds == [1, 2, 3, 4, 5]
    with pytest.raises(PermissionError):
        packed.seeds("dev")  # ...but has no hidden seeds
    if P.hidden is None:
        with pytest.raises(PermissionError):
            packed.probes(P.baseline_dir())


def test_fast_tier_only_public_seeds():
    with pytest.raises(PermissionError):
        P.evaluate(P.baseline_dir(), "fast", 123456789)


@pytest.mark.timeout(300)
def test_same_seed_is_byte_identical_without_timeouts(tmp_path):
    d = write_policy(
        tmp_path,
        "def order(queue, state):\n    return [j.id for j in sorted(queue, key=lambda j: (j.est_dur, j.id))]\n",
    )
    a = P.evaluate_many(d, [3], "fast", timeout=False)[0]
    b = P.evaluate_many(d, [3], "fast", timeout=False)[0]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


@pytest.mark.timeout(300)
def test_one_seed_six_sites_under_60s():
    t = time.time()
    r = P.evaluate(P.baseline_dir(), "fast", 2)
    assert r["score"] is not None and time.time() - t < 60


@needs_hidden
@pytest.mark.timeout(300)
def test_probes_reference_and_baseline():
    ref = {k for k, v in P.probes(P.reference_dir()).items() if v}
    assert ref == {"backfill", "admission", "locality", "packing", "est_order"}
    assert not any(P.probes(P.baseline_dir()).values())
    assert set(P.probes(P.baseline_dir())) == INGS


def test_slow_hooks_over_one_percent_void_the_run(tmp_path):
    d = write_policy(
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
    q = short_problem()
    r = q.assemble_sites([q.site_result(str(d), 1, i, tier="fast") for i in (0, 4)])
    assert r["score"] is None and r["extra"]["timed_out"] is True


def test_invalid_answers_count_as_timeouts(tmp_path):
    d = write_policy(tmp_path, "def order(queue, state):\n    return []\n")
    q = short_problem()
    r = q.assemble_sites([q.site_result(str(d), 1, 4, tier="fast")])
    assert r["score"] is None and r["extra"]["timeout_ratio"] > 0.01


def test_crashing_import_voids(tmp_path):
    d = write_policy(tmp_path, "raise RuntimeError('boom')\n")
    q = short_problem()
    r = q.assemble_sites([q.site_result(str(d), 1, 4, tier="fast")])
    assert r["score"] is None and "boom" in r["extra"]["reason"]


def test_policy_sandbox_blocks_network_and_hidden_files(tmp_path):
    hidden = (P.hidden or P.root) / "problem.yaml"
    d = write_policy(
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
    import importlib

    eng = importlib.import_module(type(P).__module__.rsplit(".", 1)[0] + ".sim.engine")
    drv = eng.PolicyDriver(d, 5, P.sites[4], timeout=True, budget_s=0.05)
    try:
        out = drv.request(
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
        drv.close()
    msg = " ".join(out["errors"])
    assert "net=open" not in msg
    if sys.platform == "darwin":
        assert "read=open" not in msg


# ------------------------------------------------------------------ two tiers of site profiles (A16-A18)


def test_fast_tier_uses_the_public_profiles_and_slow_tier_the_judges():
    common, sites = P.profiles("fast")
    assert common is P.public_common and sites is P.public_sites
    assert P.horizon("fast") == P.public_common["fast_horizon_s"]
    if P.hidden is None:
        with pytest.raises(PermissionError):
            P.profiles("slow")
    else:
        common, sites = P.profiles("slow")
        assert common is P.judge_common and sites is P.judge_sites
        assert P.sites is P.judge_sites  # probes and precheck see the judge's sites


def test_judge_machine_without_judge_sites_refuses_to_load(tmp_path, monkeypatch):
    private = tmp_path / "private" / "sched"
    private.mkdir(parents=True)
    (private / "problem.yaml").write_text("seeds: {dev: [7]}\n")  # seeds but no sites/
    monkeypatch.setenv("TB_PRIVATE", str(tmp_path / "private"))
    with pytest.raises(FileNotFoundError, match="site profiles are missing"):
        type(P)(P.root)


def test_policy_sees_only_the_plant_of_a_site(tmp_path):
    d = write_policy(
        tmp_path,
        "def order(queue, state):\n"
        "    raise RuntimeError('profile=' + ','.join(sorted(state.site_profile)))\n",
    )
    q = short_problem(hours=1)
    r = q.site_result(str(d), 1, 1, tier="fast")  # site 2, which has a mechanism
    seen = next(e for e in r["errors"] if "profile=" in e).split("profile=")[1]
    assert seen.split(",")[0:4] == ["cores_per_machine", "id", "machines", "racks"]
    assert "mechanisms" not in seen and "arrival" not in seen


@pytest.mark.timeout(300)
def test_worker_copy_reports_no_mechanism_counters(tmp_path):
    out = pack_problem("sched", tmp_path / "packed")
    scores = json.loads((out / "baseline_scores.json").read_text())
    assert all("mechanisms" not in d for d in scores["fast_detail"].values())
    packed = load_problem(out)
    r = packed.evaluate(packed.baseline_dir(), "fast", 1)
    assert "mechanisms" not in r["extra"] and "by_class" in r["extra"]
    # The site summaries describe symptoms, not the mechanisms or their numbers.
    joined = " ".join(
        yaml.safe_load(f.read_text())["summary"]
        for f in sorted((out / "sites").glob("site*.yaml"))
    ).lower()
    for giveaway in (
        "2x",
        "12 minutes",
        "20 minutes",
        "60%",
        "overload",
        "rack",
        "2.3x",
    ):
        assert giveaway not in joined, giveaway
