"""Spec 11.9 tests for problem A (the scheduling simulator), plus sandbox and packing checks."""

import importlib
import json
import sys
import time
from typing import Any

import pytest
import yaml

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
    """The public tier with a short trace, for tests that only need a few scheduling passes."""
    problem = type(PROBLEM)(PROBLEM.root)
    problem.public_common = dict(
        problem.public_common,
        horizon_s=hours * 3600,
        fast_horizon_s=hours * 3600,
        drain_cap_s=3600,
    )
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
    # The worker's site files carry the plant and the symptom only, plus the public traces (A19).
    for site_file in sorted((packed_dir / "sites").glob("*.yaml")):
        settings = yaml.safe_load(site_file.read_text())
        for key in (
            "mechanisms",
            "arrival",
            "duration",
            "users",
            "cores",
            "est_noise_sigma",
        ):
            assert key not in settings, (site_file.name, key)
    for seed in (1, 2, 3, 4, 5):
        for site in range(1, 7):
            trace = (
                packed_dir / "sites" / "traces" / f"seed{seed}" / f"site{site}.jsonl"
            )
            assert trace.exists(), trace
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
        [problem.site_result(str(candidate_dir), 1, i, tier="fast") for i in (0, 4)]
    )
    assert result["score"] is None and result["extra"]["timed_out"] is True


def test_invalid_answers_count_as_timeouts(tmp_path):
    candidate_dir = write_policy(tmp_path, "def order(queue, state):\n    return []\n")
    problem = short_problem()
    result = problem.assemble_sites(
        [problem.site_result(str(candidate_dir), 1, 4, tier="fast")]
    )
    assert result["score"] is None and result["extra"]["timeout_ratio"] > 0.01


def test_crashing_import_voids(tmp_path):
    candidate_dir = write_policy(tmp_path, "raise RuntimeError('boom')\n")
    problem = short_problem()
    result = problem.assemble_sites(
        [problem.site_result(str(candidate_dir), 1, 4, tier="fast")]
    )
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


# ------------------------------------------------------------------ two tiers of site profiles (A16-A18)


def test_fast_tier_uses_the_public_profiles_and_slow_tier_the_judges():
    common, sites = PROBLEM.profiles("fast")
    assert common is PROBLEM.public_common and sites is PROBLEM.public_sites
    assert PROBLEM.horizon("fast") == PROBLEM.public_common["fast_horizon_s"]
    if PROBLEM.hidden is None:
        with pytest.raises(PermissionError):
            PROBLEM.profiles("slow")
    else:
        common, sites = PROBLEM.profiles("slow")
        assert common is PROBLEM.judge_common and sites is PROBLEM.judge_sites
        # probes and precheck see the judge's sites
        assert PROBLEM.sites is PROBLEM.judge_sites


def test_judge_machine_without_judge_sites_refuses_to_load(tmp_path, monkeypatch):
    private = tmp_path / "private" / "sched"
    private.mkdir(parents=True)
    (private / "problem.yaml").write_text("seeds: {dev: [7]}\n")  # seeds but no sites/
    monkeypatch.setenv("TB_PRIVATE", str(tmp_path / "private"))
    with pytest.raises(FileNotFoundError, match="site profiles are missing"):
        type(PROBLEM)(PROBLEM.root)


def test_policy_sees_only_the_plant_of_a_site(tmp_path):
    candidate_dir = write_policy(
        tmp_path,
        "def order(queue, state):\n"
        "    raise RuntimeError('profile=' + ','.join(sorted(state.site_profile)))\n",
    )
    problem = short_problem(hours=1)
    # site 2, which has a mechanism
    result = problem.site_result(str(candidate_dir), 1, 1, tier="fast")
    seen = next(e for e in result["errors"] if "profile=" in e).split("profile=")[1]
    assert seen.split(",")[0:4] == ["cores_per_machine", "id", "machines", "racks"]
    assert "mechanisms" not in seen and "arrival" not in seen


@pytest.mark.timeout(300)
def test_worker_copy_reports_no_mechanism_counters(tmp_path):
    out = pack_problem("sched", tmp_path / "packed")
    scores = json.loads((out / "baseline_scores.json").read_text())
    assert all("mechanisms" not in detail for detail in scores["fast_detail"].values())
    packed = load_problem(out)
    result = packed.evaluate(packed.baseline_dir(), "fast", 1)
    assert "mechanisms" not in result["extra"] and "by_class" in result["extra"]
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
