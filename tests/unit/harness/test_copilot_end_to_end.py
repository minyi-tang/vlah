"""One real Copilot worker through the whole path (slow, manual, needs a signed-in Copilot CLI).

Worker loop -> local executor -> Copilot CLI -> the herd's MCP tool server -> the git
blackboard. A hard interrupt is pending before the session starts, so the hook must
deliver it on the first tool call. Run with: uv run pytest -m slow -k copilot_end_to_end
"""

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from tests.fixtures.executor import make_target_repository
from tests.fixtures.fakes import FakeIndexer, FakeLeases
from tests.fixtures.tools import (
    DiffingIndexer,
    RecordingRecorder,
    ScriptedEval,
    ScriptedRouter,
    serving,
)
from tests.fixtures.worker import RecordingTaskQueue
from vlah.adapters.executor.local.executor import LocalExecutor
from vlah.adapters.executor.local.passthrough import PassthroughSandbox
from vlah.adapters.harness.copilot.harness import CopilotConfig, CopilotHarness
from vlah.adapters.memory.store import MemoryObjectStore
from vlah.arms.config import PRESETS
from vlah.core.blackboard.layout import open_blackboard
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import SystemClock
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task
from vlah.core.worker.loop import InterruptInbox, WorkerDeps, run_task
from vlah.core.worker.start_pack import ContextSources
from vlah.ports.harness import Workspace
from vlah.tools.herd_tools import HerdTools

pytestmark = [
    pytest.mark.harness,
    pytest.mark.slow,
    pytest.mark.timeout(900),
    pytest.mark.skipif(shutil.which("copilot") is None, reason="needs Copilot CLI"),
]

WORKER = ActorId("worker-01")
MARKER = "INTERRUPT-SEEN"
SOLVER = (
    "def total(values):\n"
    "    result = 0\n"
    "    for value in values:\n"
    "        result += value\n"
    "    return result\n"
)
TASK = Task(
    id="t-00001",
    approach="a-01",
    kind="advance",
    goal=(
        "Make target/solver.py faster using approach a-01. Put your improved solver.py "
        "and a manifest.yaml (kind: patch, target: baseline@0, approach: a-01, author: "
        "worker-01, created_at: 0) in a directory named candidate/ in your working "
        'directory, then submit it with eval_request (predictions {"slow": 1.0}). '
        "Record what you did as an insight claim citing spec.md."
    ),
    created_by="queue",
)


def test_copilot_end_to_end(tmp_path: Path) -> None:
    clock = SystemClock()
    params = Params(heartbeat_interval_seconds=5.0)
    arm = PRESETS["S2"]
    blackboard = open_blackboard(tmp_path / "runs", "r-e2e", "blackboard")
    blackboard.write(
        ActorId("human"),
        {
            "spec.md": b"# Spec\n\nMake solver.py sum a list faster than a Python loop.\n"
        },
        "problem",
    )
    target = make_target_repository(tmp_path / "target")
    (target / "solver.py").write_text(SOLVER)
    subprocess.run(["git", "-C", str(target), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(target), "-c", "user.name=h", "-c", "user.email=h@h"]
        + ["commit", "-qm", "solver"],
        check=True,
    )
    leases = FakeLeases(clock=clock)
    router = ScriptedRouter()
    eval_service = ScriptedEval()
    objects = MemoryObjectStore()
    tools = HerdTools(
        blackboard=blackboard,
        objects=objects,
        indexer=DiffingIndexer(),
        eval_service=eval_service,
        leases=leases,
        router=router,
        recorder=RecordingRecorder(),
        arm=arm,
        params=params,
        clock=clock,
        run_id="r-e2e",
    )
    router.pending[WORKER] = Interrupt(
        id="i-e2e",
        agent=WORKER,
        kind="hard",
        rule=2,
        reason=f"Test interrupt from the herd: put the word {MARKER} in your claim.",
        written_ts=0.0,
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
            inherited_environment=os.environ,
            poll_seconds=0.5,
        )
        harness = CopilotHarness(
            config=CopilotConfig(model=os.environ.get("VLAH_E2E_MODEL", "auto")),
            params=params,
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
                target_repository=str(target),
                target_ref="main",
                blackboard_clone_url=blackboard.clone_url(),
            ),
            clock=clock,
            stop=threading.Event(),
            poll_seconds=2.0,
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
    assert claims and all("stub" not in claim.tags for claim in claims)
    assert router.acknowledged == ["i-e2e"], "the hook delivered the interrupt"
    assert any(MARKER in claim.body for claim in claims), "the agent acted on it"
    assert eval_service.requests, "the agent asked for a judge run"
    assert blackboard.list_paths(f"traces/{WORKER}/", None)
