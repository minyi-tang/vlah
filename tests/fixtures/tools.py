"""HerdTools on the real blackboard, with scripted neighbours, for the tool-server tests."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn

from tests.fixtures.fakes import FakeIndexer, FakeLeases
from tests.fixtures.herd import make_store_blackboard
from vlah.adapters.memory.store import MemoryObjectStore
from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import MergedCommit
from vlah.core.blackboard.blackboard import StoreBlackboard
from vlah.core.eval.api import EvalRequest, EvalResponse, EvalStatus, RemeasureRequest
from vlah.core.indexer.api import Diff, WorkingSet
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import Params
from vlah.tools.api import RecorderEvent
from vlah.tools.herd_tools import HerdTools
from vlah.tools.server import McpToolServer

SERVER_START_TIMEOUT_SECONDS = 10.0


@dataclass(kw_only=True)
class RecordingRecorder:
    """Records events; can hold each post at a barrier or a gate, or fail it."""

    events: list[RecorderEvent] = field(default_factory=list[RecorderEvent])
    barrier: threading.Barrier | None = None
    gate: threading.Event | None = None
    entered: threading.Event = field(default_factory=threading.Event)
    is_failing: bool = False

    def post(self, events: tuple[RecorderEvent, ...]) -> None:
        self.entered.set()
        if self.barrier is not None:
            self.barrier.wait()
        else:
            pass
        if self.gate is not None:
            self.gate.wait()
        else:
            pass
        if self.is_failing:
            raise ConnectionError("the testbed recorder is down")
        else:
            self.events.extend(events)


@dataclass(kw_only=True)
class ScriptedEval:
    """Answers each eval_request with the next scripted status.

    With a barrier, every request waits inside the service until all have arrived,
    which proves the tool server does not hold its lock across the call.
    """

    statuses: list[EvalStatus] = field(default_factory=list[EvalStatus])
    requests: list[EvalRequest] = field(default_factory=list[EvalRequest])
    barrier: threading.Barrier | None = None

    def request(self, request: EvalRequest, caller: ActorId) -> EvalResponse:
        self.requests.append(request)
        if self.barrier is not None:
            self.barrier.wait()
        else:
            pass
        status = self.statuses.pop(0) if self.statuses else "queued"
        return EvalResponse(
            request_id="request-1", claim_id=None, baseline="baseline@0", status=status
        )

    def remeasure(self, request: RemeasureRequest) -> str:
        raise NotImplementedError("the tool server never remeasures")

    def tick(self) -> tuple[Event, ...]:
        return ()


@dataclass(kw_only=True)
class DiffingIndexer(FakeIndexer):
    """Every diff renders as "diff since <cursor>"."""

    def diff_since(
        self,
        cursor: int,
        working_set: WorkingSet,
        viewer: ActorId,
        max_tokens: int,
        arm: ArmConfig,
    ) -> Diff:
        return Diff(
            items=(),
            cursor=cursor + 1,
            omitted_count=0,
            rendered=f"diff since {cursor}",
        )


@dataclass(kw_only=True)
class ScriptedRouter:
    pending: dict[ActorId, Interrupt] = field(default_factory=dict[ActorId, Interrupt])
    acknowledged: list[str] = field(default_factory=list[str])

    def on_commit(self, merged_commit: MergedCommit) -> tuple[Interrupt, ...]:
        return ()

    def check(self, agent: ActorId) -> Interrupt | None:
        return self.pending.get(agent)

    def acknowledge(self, agent: ActorId, interrupt_id: str) -> None:
        self.acknowledged.append(interrupt_id)
        self.pending.pop(agent, None)

    def overdue(self, now_seconds: float) -> tuple[Interrupt, ...]:
        return ()


@dataclass(kw_only=True)
class ToolsHarness:
    tools: HerdTools
    blackboard: StoreBlackboard
    objects: MemoryObjectStore
    leases: FakeLeases
    recorder: RecordingRecorder
    eval_service: ScriptedEval
    router: ScriptedRouter
    clock: FakeClock


def make_tools_harness(sandbox_root: Path, arm: ArmConfig) -> ToolsHarness:
    clock = FakeClock()
    blackboard = make_store_blackboard(clock, name="tools")
    objects = MemoryObjectStore()
    leases = FakeLeases(clock=clock)
    recorder = RecordingRecorder()
    eval_service = ScriptedEval()
    router = ScriptedRouter()
    tools = HerdTools(
        blackboard=blackboard,
        objects=objects,
        indexer=DiffingIndexer(),
        eval_service=eval_service,
        leases=leases,
        router=router,
        recorder=recorder,
        arm=arm,
        params=Params(),
        clock=clock,
        run_id="r-tools",
    )
    return ToolsHarness(
        tools=tools,
        blackboard=blackboard,
        objects=objects,
        leases=leases,
        recorder=recorder,
        eval_service=eval_service,
        router=router,
        clock=clock,
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@contextmanager
def serving(tools: HerdTools) -> Iterator[McpToolServer]:
    """Run the MCP tool server on a free local port for the length of the block."""
    server = McpToolServer(tools=tools, host="127.0.0.1", port=free_port())
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            server.app(), host=server.host, port=server.port, log_level="warning"
        )
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while not uvicorn_server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert uvicorn_server.started, "the tool server did not start"
    try:
        yield server
    finally:
        uvicorn_server.should_exit = True
        thread.join(SERVER_START_TIMEOUT_SECONDS)
