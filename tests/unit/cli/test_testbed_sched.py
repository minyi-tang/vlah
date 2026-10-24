"""The scheduler template agrees with public testbed settings and stays unsigned."""

import shutil
from pathlib import Path

import pytest

from problems.sched.problem import DEFAULT_KNOBS
from testbed.integrator.core import IntegratorConfig
from vlah.core.problem.loader import load_problem

pytestmark = pytest.mark.cli

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEDULER_FOLDER = REPOSITORY_ROOT / "examples/testbed-sched"
HOURS_PER_DAY = 24


def test_scheduler_quota_and_integrator_match_the_public_testbed() -> None:
    problem = load_problem(SCHEDULER_FOLDER)
    assert len(problem.sources) == 1
    source = problem.sources[0]
    assert source.id == "slow"
    assert source.mode == "pull"
    assert source.latency_seconds == DEFAULT_KNOBS["visible_delay_s"]
    assert source.noise.n_for_ci == DEFAULT_KNOBS["slow_seeds"]
    assert source.budget.per_day == DEFAULT_KNOBS["quota_per_hour"] * HOURS_PER_DAY
    assert source.budget.allocated_by == "human"
    assert problem.limits.runs == {"slow": DEFAULT_KNOBS["quota_total"]}
    assert problem.limits.days == DEFAULT_KNOBS["quota_total"] / (
        DEFAULT_KNOBS["quota_per_hour"] * HOURS_PER_DAY
    )
    assert problem.herd_config.judges["slow"].adapter_class == (
        "vlah.adapters.signal.testbed_http.client:SlowTierSource"
    )
    assert problem.herd_config.judges["slow"].command is None

    integrator = IntegratorConfig()
    params = problem.params
    assert params.accept_rule == "default"
    assert params.accept_min_standard_errors == integrator.accept_se
    assert params.interaction_sigma_multiple == integrator.interaction_k
    assert params.spot_check_every_merges == integrator.audit_every
    assert params.spot_check_sigma_multiple == integrator.audit_k
    assert params.spot_check_reserved_for_bisection == integrator.audit_reserve
    assert (
        params.holdout_run_cap == integrator.audit_max == DEFAULT_KNOBS["audit_total"]
    )
    assert params.verdict_repeat_count == DEFAULT_KNOBS["judge_total"]
    assert params.quota_integrator_total == DEFAULT_KNOBS["quota_integrator_total"]
    assert (
        params.quota_integrator_per_hour == DEFAULT_KNOBS["quota_integrator_per_hour"]
    )
    assert problem.gates == ()


def test_scheduler_keeps_approaches_and_operator_choices_in_draft() -> None:
    problem = load_problem(SCHEDULER_FOLDER)
    assert problem.status == "draft"
    assert problem.target is None
    assert problem.params.slots_per_approach == {
        "order": 1,
        "admit": 1,
        "place": 1,
        "preempt": 1,
    }
    assert {approach.id for approach in problem.approaches} == set(
        problem.params.slots_per_approach
    )
    assert all(
        approach.status == "active" and approach.parent is None
        for approach in problem.approaches
    )
    assert problem.sources[0].trust == 0
    assert problem.sources[0].readable_by == ()
    assert problem.sources[0].concurrency is None
    assert b"[ ] slow" in problem.public_files.files["charter/sources.md"]
    assert problem.herd_config.executor.settings["target_repository"] == (
        "__TARGET_REPOSITORY__"
    )
    existing_example = load_problem(
        REPOSITORY_ROOT / "examples/prompt_optimization/problem"
    )
    assert problem.herd_config.harness == existing_example.herd_config.harness
    assert problem.herd_config.models == existing_example.herd_config.models


def test_scheduler_public_view_excludes_deployment_and_private_bytes(
    tmp_path: Path,
) -> None:
    root = shutil.copytree(SCHEDULER_FOLDER, tmp_path / "scheduler")
    deployment = root / "herd.yaml"
    deployment.write_text(
        deployment.read_text(encoding="utf-8") + "\n# PRIVATE_DEPLOYMENT_SENTINEL\n",
        encoding="utf-8",
    )
    private = root / "private"
    private.mkdir()
    (private / "operator.txt").write_text("PRIVATE_DATA_SENTINEL\n", encoding="utf-8")

    problem = load_problem(root)
    assert "spec.md" in problem.public_files.files
    assert "map/gap.md" in problem.public_files.files
    assert "eval/sources/slow.yaml" in problem.public_files.files
    assert all(
        "private" not in Path(path).parts and Path(path).name != "herd.yaml"
        for path in problem.public_files.files
    )
    assert all(
        b"PRIVATE_" not in content for content in problem.public_files.files.values()
    )
