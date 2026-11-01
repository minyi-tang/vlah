"""Testbed worker credentials and the actual macOS file-read boundary."""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from random import Random

import pytest
from pydantic import JsonValue

from tests.fixtures.runtime import toy_problem
from vlah.adapters.executor.local.passthrough import PassthroughSandbox
from vlah.adapters.memory.executor import ScriptedHarness
from vlah.adapters.signal.testbed_http.client import HttpReply, SlowTierClient
from vlah.arms.config import PRESETS
from vlah.cli import main
from vlah.core.integrator.core_adapter import config_from_params
from vlah.core.problem.api import AdapterChoice, JudgeConfig
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy
from vlah.core.types.tasks import Task
from vlah.ports.errors import NotSupportedError
from vlah.ports.executor import AgentSpec, StartPack
from vlah.ports.harness import HookConfig, ToolEndpoint, Workspace
from vlah.runtime.assembly import RunComponents, RunContext
from vlah.runtime.local_assembly import LocalAssembly
from vlah.runtime.testbed_assembly import TestbedAssembly as ArmAssembly
from vlah.runtime.testbed_problem import SLOW_ADAPTER
from vlah.runtime.testbed_sandbox import TestbedSandbox as WorkerSandbox
from vlah.runtime.testbed_sandbox import stage_worker_runtime
from vlah.runtime.testbed_workers import TestbedHarness as WorkerHarness

pytestmark = pytest.mark.runtime

READ_PROBE = """import json, os, subprocess, sys
from pathlib import Path
import vlah
import testbed
root = Path(os.environ["VLAH_SANDBOX"])
assert (root / "public.txt").read_text() == "public"
(Path.home() / "note.txt").write_text("mine")
assert Path(vlah.__file__).is_relative_to(Path(os.environ["PYTHONPATH"]))
assert Path(testbed.__file__).is_relative_to(Path(os.environ["PYTHONPATH"]))
for filename in json.loads(sys.argv[1]):
    try:
        Path(filename).read_bytes()
    except PermissionError:
        pass
    else:
        raise RuntimeError("operator or other worker files were readable")
for module in ("testbed.slow_eval", "testbed.extras"):
    try:
        __import__(module)
    except ModuleNotFoundError:
        pass
    else:
        raise RuntimeError("operator testbed modules were packaged")
try:
    result = subprocess.run(
        ["/bin/ps", "-p", str(os.getppid())], capture_output=True, text=True
    )
except PermissionError:
    pass
else:
    assert str(os.getppid()) not in result.stdout
print("isolated")
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS boundary")
def test_worker_cannot_read_the_repo_operator_files_or_another_worker(
    tmp_path: Path,
) -> None:
    """The generic egress sandbox left operator keys, calibration and the source repo readable."""
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "public.txt").write_text("public")
    private_files = [
        tmp_path / "operator" / "system.key",
        tmp_path / "run" / "calibration.json",
        tmp_path / "other-worker" / "transcript.jsonl",
    ]
    for path in private_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not public")
    private_files.append(Path(__file__).resolve().parents[3] / "pyproject.toml")
    runtime = stage_worker_runtime(tmp_path / "worker-runtime")
    sandbox = WorkerSandbox(
        clock=FakeClock(),
        work_directory=tmp_path / "egress",
        worker_runtime=runtime,
    )
    environment = {
        "PATH": os.environ["PATH"],
        "VLAH_SANDBOX": str(worker),
        "HOME": str(worker / ".home"),
        "TMPDIR": str(worker / ".tmp"),
        "PYTHONPATH": str(runtime),
        "PYTHONNOUSERSITE": "1",
    }
    try:
        wrapped = sandbox.wrap(
            ActorId("worker-01"),
            (
                sys.executable,
                "-c",
                READ_PROBE,
                json.dumps([str(path) for path in private_files]),
            ),
            environment,
            EgressPolicy(),
        )
        completed = subprocess.run(
            wrapped.argv,
            env=wrapped.env,
            cwd=worker,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "isolated"
        assert not (runtime / "testbed" / "slow_eval").exists()
        assert not (runtime / "testbed" / "integrator").exists()
    finally:
        sandbox.close()


@pytest.mark.parametrize("layer", ["none", "linux-namespaces"])
def test_inadequate_filesystem_isolation_fails_closed(
    tmp_path: Path, layer: str
) -> None:
    sandbox = WorkerSandbox(
        clock=FakeClock(),
        work_directory=tmp_path / "egress",
        worker_runtime=tmp_path / "runtime",
        layer_two=layer,
    )
    with pytest.raises(NotSupportedError, match="read-allowlist"):
        sandbox.require_enforcement()


def test_sandbox_allowlist_cannot_reopen_the_source_repository(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[3]
    sandbox = WorkerSandbox(
        clock=FakeClock(),
        work_directory=tmp_path / "egress",
        worker_runtime=tmp_path / "runtime",
        layer_two="macos-sandbox-exec",
        runtime_read_paths=(source,),
    )
    with pytest.raises(ValueError, match="source repository"):
        sandbox.require_enforcement()


def test_sandbox_allowlist_cannot_reopen_operator_files(tmp_path: Path) -> None:
    operator = tmp_path / "operator"
    operator.mkdir()
    sandbox = WorkerSandbox(
        clock=FakeClock(),
        work_directory=tmp_path / "egress",
        worker_runtime=tmp_path / "runtime",
        layer_two="macos-sandbox-exec",
        protected_directories=(operator,),
        runtime_read_paths=(tmp_path,),
    )
    with pytest.raises(ValueError, match="operator files"):
        sandbox.require_enforcement()


class CredentialHarness(ScriptedHarness):
    environment: dict[str, str]

    def __init__(self, environment: dict[str, str]) -> None:
        super().__init__(default_program="fixture")
        self.environment = environment

    def build(
        self,
        agent: ActorId,
        task: Task,
        start_pack: StartPack,
        tool_endpoint: ToolEndpoint,
        hook_config: HookConfig,
        workspace: Workspace,
    ) -> AgentSpec:
        spec = super().build(
            agent, task, start_pack, tool_endpoint, hook_config, workspace
        )
        return spec.model_copy(update={"env": {**spec.env, **self.environment}})


@pytest.mark.parametrize(
    "credential", ["TB_SYSTEM_KEY", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"]
)
def test_harness_rejects_operator_credentials_even_when_explicitly_configured(
    tmp_path: Path,
    credential: str,
) -> None:
    harness = WorkerHarness(
        harness=CredentialHarness({credential: "operator-credential"}),
        slow_eval_url="http://127.0.0.1:8123",
        run_id="r-test",
        tokens={ActorId("worker-01"): "own-token"},
        sandbox_root=tmp_path,
        worker_runtime=tmp_path / "runtime",
        problem_files={},
        egress=EgressPolicy(),
        clock=FakeClock(),
    )
    with pytest.raises(ValueError, match="operator credentials"):
        harness.build(
            ActorId("worker-01"),
            Task(
                id="t-1",
                approach="order",
                kind="advance",
                goal="fixture",
                created_by="queue",
            ),
            StartPack(files={}),
            ToolEndpoint(url="http://127.0.0.1:8124/mcp", bearer_token="tools-token"),
            HookConfig(
                interrupt_check_url="http://127.0.0.1:8124/hook/check",
                interrupt_ack_url="http://127.0.0.1:8124/hook/ack",
            ),
            Workspace(
                target_repository=str(tmp_path),
                target_ref="HEAD",
                blackboard_clone_url="fixture",
            ),
        )


@dataclass(frozen=True, kw_only=True)
class RegistrationClient(SlowTierClient):
    receipt: HttpReply

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, JsonValue] | None,
        actor: ActorId | None = None,
    ) -> HttpReply:
        assert (method, path, actor) == ("POST", "/actors", None)
        assert body is not None and body["scope"] == "fast"
        return self.receipt


@pytest.mark.parametrize(
    "receipt",
    [
        HttpReply(status=404, body={}),
        HttpReply(
            status=200,
            body={
                "run_id": "r-refused",
                "actors": ["worker-00", "worker-01", "worker-02"],
            },
        ),
        HttpReply(
            status=200,
            body={
                "run_id": "r-refused",
                "actors": ["worker-00", "worker-01", "worker-02"],
                "scopes": {"worker-00": "fast", "worker-01": "fast"},
            },
        ),
        HttpReply(
            status=200,
            body={
                "run_id": "r-refused",
                "actors": ["worker-00", "worker-01", "worker-02"],
                "scopes": {
                    "worker-00": "full",
                    "worker-01": "full",
                    "worker-02": "full",
                },
            },
        ),
    ],
    ids=["missing-endpoint", "old-server", "missing-actor-scope", "ordinary-token"],
)
def test_registration_must_acknowledge_fast_only_scope_before_building_components(
    tmp_path: Path,
    receipt: HttpReply,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older server can ignore the scope field and return ordinary credentials with HTTP 200."""
    problem = toy_problem(tmp_path)
    problem = replace(
        problem,
        herd_config=problem.herd_config.model_copy(
            update={
                "judges": {
                    problem.sources[0].id: JudgeConfig(adapter_class=SLOW_ADAPTER)
                },
                "executor": AdapterChoice(kind="local"),
                "harness": AdapterChoice(kind="copilot"),
            }
        ),
    )
    assembly = ArmAssembly(
        client=RegistrationClient(
            base_url="http://127.0.0.1:1",
            run_id="r-refused",
            system_key="fixture",
            receipt=receipt,
        ),
        integrator_config=config_from_params(problem.params),
        worker_count=2,
    )
    context = RunContext(
        run_id="r-refused",
        run_root=tmp_path / "run",
        arm=PRESETS["S2"],
        params=problem.params,
        problem=problem,
        clock=FakeClock(),
        random=Random(0),
        duration_seconds=1,
    )

    def fixture_sandbox(
        assembly: ArmAssembly, context: RunContext
    ) -> PassthroughSandbox:
        return PassthroughSandbox()

    def forbid_components(
        assembly: LocalAssembly, context: RunContext
    ) -> RunComponents:
        raise AssertionError("an unscoped registration must not build run components")

    monkeypatch.setattr(ArmAssembly, "make_sandbox", fixture_sandbox)
    monkeypatch.setattr(LocalAssembly, "build", forbid_components)
    with pytest.raises(ValueError, match="token registration|scopes|fast-only"):
        assembly.build(context)
    assert assembly.parts == []


@pytest.mark.parametrize(
    "flag,value,error",
    [
        ("--workers", "0", "--workers must be positive"),
        ("--hours", "nan", "--hours must be finite"),
        ("--run-id", "../escape", "--run-id must be a safe directory"),
    ],
)
def test_arm_cli_rejects_unsafe_run_arguments_before_loading_files(
    tmp_path: Path,
    flag: str,
    value: str,
    error: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "arm",
                "run",
                "--arm",
                "S2",
                "--problem",
                str(tmp_path / "missing-pack"),
                "--slow-url",
                "http://127.0.0.1:1",
                "--run-id",
                "r-test",
                "--workers",
                "2",
                "--hours",
                "1",
                "--integrator-config",
                str(tmp_path / "missing-config"),
                flag,
                value,
            ]
        )
        == 2
    )
    assert error in capsys.readouterr().err
