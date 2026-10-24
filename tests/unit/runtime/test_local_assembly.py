"""The composition root over the real adapters, built from the prompt-optimization example."""

import socket
import subprocess
import sys
from pathlib import Path
from random import Random

import pytest

from vlah.adapters.executor.sandbox.sandbox import ProcessSandbox
from vlah.arms.config import PRESETS
from vlah.core.integrator.service import HerdIntegrator
from vlah.core.problem.loader import load_problem
from vlah.core.types.clock import FakeClock
from vlah.runtime.assembly import RunContext
from vlah.runtime.command import RunRequest, run_problem
from vlah.runtime.local_assembly import LocalAssembly, export_baseline

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
    assert set(problem.params.slots_per_approach) == {"a-01", "a-02"}
    assert "private/judge.py" not in problem.public_files.files
    assert "herd.yaml" not in problem.public_files.files
    baseline = export_baseline(root / "target", "main", tmp_path / "baseline0")
    assert sorted(path.name for path in baseline.iterdir()) == ["prompt.md"]


def test_the_local_assembly_builds_every_real_component(tmp_path: Path) -> None:
    root = example_problem(tmp_path)
    problem = load_problem(root)
    clock = FakeClock()
    context = RunContext(
        run_id="r-1",
        run_root=root / ".herd" / "runs" / "r-1",
        arm=PRESETS["S2"],
        params=problem.params,
        problem=problem,
        clock=clock,
        random=Random(0),
        duration_seconds=60.0,
    )
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
