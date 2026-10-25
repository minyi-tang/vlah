"""The composition root over the real adapters, built from the prompt-optimization example."""

import dataclasses
import json
import socket
import subprocess
import sys
from pathlib import Path
from random import Random

import pytest

from tests.fixtures.runtime import toy_problem
from vlah.adapters.executor.sandbox.sandbox import ProcessSandbox
from vlah.adapters.harness.claude_code.harness import ClaudeCodeHarness
from vlah.adapters.store.redis.store import RedisKeyValueStore
from vlah.arms.config import PRESETS
from vlah.core.integrator.service import HerdIntegrator
from vlah.core.problem.api import AdapterChoice, Problem
from vlah.core.problem.loader import load_problem
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.runtime.assembly import RunContext
from vlah.runtime.command import RunRequest, run_problem
from vlah.runtime.local_assembly import (
    LocalAssembly,
    export_baseline,
    key_value_store,
    worker_harness,
)

pytestmark = pytest.mark.runtime

REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP = REPO_ROOT / "examples" / "prompt_optimization" / "setup.py"
BASELINE_DAILY_SCORE = 0.125


def example_problem(tmp_path: Path) -> Path:
    destination = tmp_path / "prompt-opt"
    subprocess.run([sys.executable, str(SETUP), str(destination)], check=True)
    return destination


def test_the_example_problem_loads_and_its_target_exports(tmp_path: Path) -> None:
    root = example_problem(tmp_path)
    problem = load_problem(root)
    assert problem.status == "aligned"
    assert [source.id for source in problem.sources] == ["rubric"]
    assert set(problem.params.slots_per_approach) == {"a-01", "a-02", "a-03", "a-04"}
    assert "private/judge.py" not in problem.public_files.files
    assert "herd.yaml" not in problem.public_files.files
    baseline = export_baseline(root / "target", "main", tmp_path / "baseline0")
    assert sorted(path.name for path in baseline.iterdir()) == ["prompt.md"]


def run_context(problem: Problem, run_root: Path) -> RunContext:
    return RunContext(
        run_id="r-1",
        run_root=run_root,
        arm=PRESETS["S2"],
        params=problem.params,
        problem=problem,
        clock=FakeClock(),
        random=Random(0),
        duration_seconds=60.0,
    )


def test_the_local_assembly_builds_every_real_component(tmp_path: Path) -> None:
    root = example_problem(tmp_path)
    problem = load_problem(root)
    context = run_context(problem, root / ".herd" / "runs" / "r-1")
    assembly = LocalAssembly(allow_unenforced_egress=True, executor_poll_seconds=0.5)
    components = assembly.build(context)
    try:
        assert components.blackboard.list_paths("spec.md", None) == ("spec.md",)
        assert "herd.yaml" not in components.blackboard.list_paths("", None)
        integrator = components.integrator
        assert isinstance(integrator, HerdIntegrator)
        assert integrator.calibration.score == BASELINE_DAILY_SCORE
        assert integrator.calibration.audit_per_seed is not None
        assert integrator.current().k == 0
        assert (context.run_root / "baseline0" / "prompt.md").exists()
        assert components.target_repository == str(root / "target")
        executor = assembly.parts[0].executor
        assert executor.sandbox_root == root / ".herd" / "sandboxes" / "r-1"
        assert not executor.sandbox_root.is_relative_to(context.run_root)
        assert isinstance(executor.sandbox, ProcessSandbox)
        protected = executor.sandbox.protected_directories
        assert context.run_root in protected and root / "private" in protected
        assert root / "target" in protected and root / "herd.yaml" in protected
        for written_by_workers in (executor.sandbox_root, executor.log_root):
            assert not any(
                written_by_workers.is_relative_to(directory) for directory in protected
            ), f"a worker must be able to write {written_by_workers}"
        assert executor.log_root == root / ".herd" / "logs" / "r-1"
        for served in assembly.parts[0].tool_servers:
            with socket.create_connection((served.server.host, served.server.port), 2):
                pass
        assert components.tool_server is not components.compose_tool_server
        assert components.eval_service is integrator.eval_service
    finally:
        assembly.close()


def test_a_rebuilt_run_keeps_its_store_and_its_calibration(tmp_path: Path) -> None:
    """The store lived in the process and baseline@0 was measured at every start, so
    a resumed run lost its queue and spent two judge runs (#120). The second build's
    judge fails, so only a calibration read back lets it build."""
    root = example_problem(tmp_path)
    problem = load_problem(root)
    context = run_context(problem, root / ".herd" / "runs" / "r-1")
    first_assembly = LocalAssembly(allow_unenforced_egress=True)
    try:
        first = first_assembly.build(context)
        first.task_queue.put(
            Task(
                id="t-kept",
                approach="a-01",
                kind="advance",
                goal="g",
                created_by="queue",
            )
        )
        assert isinstance(first.integrator, HerdIntegrator)
        calibration = first.integrator.calibration
    finally:
        first_assembly.close()
    broken_judges = {
        source_id: judge.model_copy(update={"command": "exit 1"})
        for source_id, judge in problem.herd_config.judges.items()
    }
    broken_problem = dataclasses.replace(
        problem,
        herd_config=problem.herd_config.model_copy(update={"judges": broken_judges}),
    )
    second_assembly = LocalAssembly(allow_unenforced_egress=True)
    try:
        second = second_assembly.build(
            dataclasses.replace(context, problem=broken_problem)
        )
        assert isinstance(second.integrator, HerdIntegrator)
        assert second.integrator.calibration == calibration
        kept = second.task_queue.claim_task(ActorId("worker-01"))
        assert kept is not None and kept.id == "t-kept"
        assert (context.run_root / "state.sqlite").is_file()
    finally:
        second_assembly.close()


def test_herd_yaml_can_keep_the_run_state_in_redis(tmp_path: Path) -> None:
    problem = toy_problem(tmp_path)
    herd_config = problem.herd_config.model_copy(
        update={
            "stores": {
                "key_value": AdapterChoice(
                    kind="redis", settings={"url": "redis://redis.example:6390/2"}
                )
            }
        }
    )
    context = run_context(problem, tmp_path / "r-1")
    store = key_value_store(herd_config, context)
    assert isinstance(store, RedisKeyValueStore)
    connection = store.client.connection_pool.connection_kwargs
    assert (connection["host"], connection["port"], connection["db"]) == (
        "redis.example",
        6390,
        2,
    )
    assert store.namespace == context.run_id


def test_run_neither_reuses_a_run_id_nor_resumes_a_missing_run(tmp_path: Path) -> None:
    root = example_problem(tmp_path)
    runs_root = tmp_path / "runs"
    with pytest.raises(ValueError, match="no run r-missing to resume"):
        run_problem(
            RunRequest(
                problem_root=root,
                arm="S2",
                worker_count=1,
                duration_hours=0.01,
                run_id="r-missing",
                runs_root=runs_root,
                is_resume=True,
            )
        )
    (runs_root / "r-taken").mkdir(parents=True)
    with pytest.raises(ValueError, match="--resume r-taken"):
        run_problem(
            RunRequest(
                problem_root=root,
                arm="S2",
                worker_count=1,
                duration_hours=0.01,
                run_id="r-taken",
                runs_root=runs_root,
            )
        )


def test_run_refuses_an_unsigned_problem_or_one_without_slots(tmp_path: Path) -> None:
    root = example_problem(tmp_path)
    (root / "charter" / "status.md").write_text("draft\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sign the charter"):
        run_problem(
            RunRequest(problem_root=root, arm="S2", worker_count=1, duration_hours=0.01)
        )
    (root / "charter" / "status.md").write_text("aligned\n", encoding="utf-8")
    (root / "charter" / "params.yaml").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="slots_per_approach"):
        run_problem(
            RunRequest(problem_root=root, arm="S2", worker_count=1, duration_hours=0.01)
        )


def test_the_example_can_run_claude_code_workers_and_a_model_graded_judge(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "prompt-opt-claude"
    subprocess.run(
        [
            sys.executable,
            str(SETUP),
            str(destination),
            "--judge",
            "llm",
            "--harness",
            "claude_code",
            "--claude-command",
            "/opt/gateway/claude-ghcp",
            "--claude-bin",
            "/opt/claude",
            "--model-api-url",
            "http://127.0.0.1:4000",
            "--effort",
            "low",
            "--slots",
            "2",
        ],
        check=True,
    )
    problem = load_problem(destination)
    assert problem.params.slots_per_approach == {
        "a-01": 2,
        "a-02": 2,
        "a-03": 2,
        "a-04": 2,
    }
    judge = problem.herd_config.judges["rubric"].command
    assert judge is not None and "private/llm_judge.py" in judge
    assert (destination / "private" / "llm_judge.py").is_file()
    harness = worker_harness(problem.herd_config, problem.params, ("rubric",))
    assert isinstance(harness, ClaudeCodeHarness)
    assert harness.config.claude_command == ("/opt/gateway/claude-ghcp",)
    assert harness.config.environment == {"CLAUDE_BIN": "/opt/claude"}
    assert harness.config.model == "claude-opus-5-5"
    assert harness.config.effort == "low"
    assert harness.config.model_api_urls == ("http://127.0.0.1:4000",)


def test_the_model_graded_judge_fails_a_long_prompt_without_calling_a_model(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "prompt.md").write_text("word " * 400, encoding="utf-8")
    judge = REPO_ROOT / "examples" / "prompt_optimization" / "judge" / "llm_judge.py"
    completed = subprocess.run(
        [sys.executable, str(judge), str(candidate), "--dataset", "dev"],
        env={"PATH": "/usr/bin:/bin", "VLAH_JUDGE_BASE_URL": "http://127.0.0.1:1"},
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["constraint_ok"] is False and result["score"] == 0.0
    assert result["words"] == 400 and len(result["per_seed"]) == 8
