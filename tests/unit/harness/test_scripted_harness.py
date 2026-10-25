"""The scripted worker harness: the port's contract, and a whole task through the
worker loop, the local executor, the tool server and the blackboard with no model (#127)."""

import os
import threading
from pathlib import Path

import pytest

from tests.fixtures.eval_harness import make_eval_harness
from tests.fixtures.executor import make_target_repository
from tests.fixtures.fakes import FakeIndexer, FakeLeases
from tests.fixtures.tools import (
    DiffingIndexer,
    RecordingRecorder,
    ScriptedRouter,
    serving,
)
from tests.fixtures.worker import RecordingTaskQueue
from vlah.adapters.executor.local.executor import LocalExecutor
from vlah.adapters.executor.local.passthrough import PassthroughSandbox
from vlah.adapters.harness.scripted.harness import (
    ScriptedWorkerConfig,
    ScriptedWorkerHarness,
)
from vlah.adapters.memory.store import MemoryObjectStore
from vlah.arms.config import PRESETS
from vlah.core.blackboard.layout import open_blackboard
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import SystemClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task
from vlah.core.worker.loop import InterruptInbox, WorkerDeps, run_task
from vlah.core.worker.start_pack import ContextSources
from vlah.ports.executor import StartPack
from vlah.ports.harness import HookConfig, ToolEndpoint, Workspace
from vlah.testing.conformance import HarnessCheckInputs, check_harness
from vlah.testing.scripted_worker import ScriptedTranscript
from vlah.tools.herd_tools import HerdTools

pytestmark = pytest.mark.harness

WORKER = ActorId("worker-01")
TASK = Task(
    id="t-00001",
    approach="a-01",
    kind="advance",
    goal="Advance a-01",
    created_by="queue",
)
TOOL_ENDPOINT = ToolEndpoint(url="http://127.0.0.1:8200/mcp", bearer_token="token-1")
HOOK_CONFIG = HookConfig(
    interrupt_check_url="http://127.0.0.1:8200/hook/check",
    interrupt_ack_url="http://127.0.0.1:8200/hook/ack",
)
WORKSPACE = Workspace(
    target_repository="/repositories/target",
    target_ref="main",
    blackboard_clone_url="file:///repositories/blackboard.git",
)


def test_the_harness_meets_the_port_and_names_the_session(tmp_path: Path) -> None:
    harness = ScriptedWorkerHarness(
        config=ScriptedWorkerConfig(
            source_ids=("fast", "slow"), python_executable="/image/python"
        )
    )
    transcript = tmp_path / "transcript.json"
    transcript.write_text("")
    check_harness(
        harness,
        HarnessCheckInputs(
            agent=WORKER,
            task=TASK,
            start_pack=StartPack(files={"CONTEXT.md": "# Task"}),
            tool_endpoint=TOOL_ENDPOINT,
            hook_config=HOOK_CONFIG,
            workspace=WORKSPACE,
            transcript_path=transcript,
        ),
    )
    spec = harness.build(
        WORKER,
        TASK.model_copy(
            update={"kind": "result_followup", "refs": ("c-eval_service-00001",)}
        ),
        StartPack(files={}),
        TOOL_ENDPOINT,
        HOOK_CONFIG,
        WORKSPACE,
    )
    assert spec.command[:3] == ("/image/python", "-m", "vlah.testing.scripted_worker")
    assert spec.env["VLAH_TASK_KIND"] == "result_followup"
    assert spec.env["VLAH_TASK_REFS"] == "c-eval_service-00001"
    assert spec.env["VLAH_SOURCES"] == "fast,slow"
    assert spec.egress is not None
    assert {rule.host for rule in spec.egress.allow} == {
        "127.0.0.1"
    }, "only the herd's tools"
    transcript.write_text(
        ScriptedTranscript(
            used_tokens=3000,
            window_tokens=10_000,
            tool_calls=("eval_request", "repo_write"),
        ).model_dump_json()
    )
    assert harness.usage(transcript).fraction == pytest.approx(0.3)
    assert harness.recent_tool_calls(transcript, 1) == ("repo_write",)


def test_a_task_runs_through_the_loop_with_no_model(tmp_path: Path) -> None:
    """Worker loop -> local executor -> scripted worker -> MCP tool server -> eval
    client -> git blackboard: the LLM-free path every executor in the local track reuses.
    """
    clock = SystemClock()
    params = Params(heartbeat_interval_seconds=1.0)
    arm = PRESETS["S2"]
    blackboard = open_blackboard(tmp_path / "runs", "r-scripted", "blackboard")
    blackboard.write(ActorId("human"), {"spec.md": b"# Spec\n"}, "problem")
    eval_harness = make_eval_harness(tmp_path / "eval", blackboard=blackboard)
    leases = FakeLeases(clock=clock)
    objects = MemoryObjectStore()
    tools = HerdTools(
        blackboard=blackboard,
        objects=objects,
        indexer=DiffingIndexer(),
        eval_service=eval_harness.service,
        leases=leases,
        router=ScriptedRouter(),
        recorder=RecordingRecorder(),
        arm=arm,
        params=params,
        clock=clock,
        run_id="r-scripted",
    )
    task_queue = RecordingTaskQueue()
    with serving(tools) as server:
        executor = LocalExecutor(
            sandbox_root=tmp_path / "sandboxes",
            log_root=tmp_path / "logs",
            blackboard=blackboard,
            objects=objects,
            claims=server,
            sandbox=PassthroughSandbox(),
            params=params,
            clock=clock,
            inherited_environment={"PATH": os.environ.get("PATH", "")},
            poll_seconds=0.2,
        )
        harness = ScriptedWorkerHarness(
            config=ScriptedWorkerConfig(source_ids=("slow",))
        )
        deps = WorkerDeps(
            task_queue=task_queue,
            leases=leases,
            executor=executor,
            harness=harness,
            tool_server=server,
            context=ContextSources(
                blackboard=blackboard, indexer=FakeIndexer(), arm=arm, params=params
            ),
            inbox=InterruptInbox(),
            workspace=Workspace(
                target_repository=str(make_target_repository(tmp_path / "target")),
                target_ref="main",
                blackboard_clone_url=blackboard.clone_url(),
            ),
            clock=clock,
            stop=threading.Event(),
            poll_seconds=0.2,
        )
        try:
            run_task(WORKER, TASK, deps, cursor=0)
        finally:
            executor.close()

    claims = [
        parse_claim(blackboard.read(path, None).decode())
        for path in blackboard.list_paths(f"claims/{WORKER}/", None)
    ]
    assert task_queue.completed == [TASK.id]
    assert [claim.type for claim in claims] == ["insight"]
    assert all("stub" not in claim.tags for claim in claims)
    assert (
        len(blackboard.list_paths("claims/eval_service/", None)) == 1
    ), "one judge run"
    assert blackboard.list_paths(f"traces/{WORKER}/", None), "the trace was archived"
