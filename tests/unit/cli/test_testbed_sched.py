"""The scheduler template loads without simulator dependencies and stays unsigned."""

import ast
import shutil
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from testbed.integrator.core import IntegratorConfig
from vlah.core.problem.loader import load_problem

pytestmark = pytest.mark.cli

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEDULER_FOLDER = REPOSITORY_ROOT / "examples/testbed-sched"
HOURS_PER_DAY = 24


def test_scheduler_quota_and_integrator_match_the_public_testbed() -> None:
    public_module = ast.parse(
        (REPOSITORY_ROOT / "problems/sched/problem.py").read_text(encoding="utf-8")
    )
    knob_assignment = next(
        statement
        for statement in public_module.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_KNOBS"
            for target in statement.targets
        )
    )
    public_knobs = TypeAdapter(dict[str, int]).validate_python(
        ast.literal_eval(knob_assignment.value)
    )
    problem = load_problem(SCHEDULER_FOLDER)
    assert len(problem.sources) == 1
    source = problem.sources[0]
    assert source.id == "slow"
    assert source.mode == "pull"
    assert source.latency_seconds == public_knobs["visible_delay_s"]
    assert source.noise.n_for_ci == public_knobs["slow_seeds"]
    assert source.budget.per_day == public_knobs["quota_per_hour"] * HOURS_PER_DAY
    assert source.budget.allocated_by == "human"
    assert problem.limits.runs == {"slow": public_knobs["quota_total"]}
    assert problem.limits.days == public_knobs["quota_total"] / (
        public_knobs["quota_per_hour"] * HOURS_PER_DAY
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
    assert params.holdout_run_cap == integrator.audit_max == public_knobs["audit_total"]
    assert params.verdict_repeat_count == public_knobs["judge_total"]
    assert params.quota_integrator_total == public_knobs["quota_integrator_total"]
    assert params.quota_integrator_per_hour == public_knobs["quota_integrator_per_hour"]
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
