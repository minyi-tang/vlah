"""The worker loop on scripted agents: fake queue and leases, Copilot-format transcripts."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from tests.fixtures.fakes import FakeIndexer, FakeLeases, FakeTaskQueue
from tests.fixtures.herd import make_store_blackboard
from vlah.adapters.harness.copilot.transcript import (
    latest_context,
    rate_limit_events,
    tool_calls,
)
from vlah.adapters.memory.executor import (
    MemoryExecutor,
    ScriptedHarness,
    ScriptedProgram,
)
from vlah.arms.config import ArmConfig
from vlah.core.blackboard.blackboard import StoreBlackboard
from vlah.core.indexer.api import Diff, WorkingSet
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.tasks import Lease, Task
from vlah.core.worker.loop import InterruptInbox, WorkerDeps
from vlah.core.worker.start_pack import ContextSources
from vlah.ports.executor import AgentSpec, Handle, StartPack
from vlah.ports.harness import ContextUsage, HookConfig, ToolEndpoint, Workspace

WORKER = ActorId("worker-01")
WINDOW_TOKENS = 200_000
TRANSCRIPT_VARIABLE = "TEST_TRANSCRIPT"
TASK = Task(
    id="t-00001",
    approach="a-01",
    kind="advance",
    goal="Make the solver faster.",
    created_by="queue",
)


def assistant_line(context_tokens: int, tool_name: str | None = None) -> str:
    """Copilot events for one model call using this many context tokens."""
    events: list[dict[str, object]] = []
    if tool_name is not None:
        events.append(
            {
                "type": "tool.execution_start",
                "data": {"toolName": tool_name, "arguments": {"command": "ls"}},
            }
        )
    else:
        pass
    events.append(
        {
            "type": "session.usage_record",
            "data": {
                "usage": {
                    "inputTokens": context_tokens,
                    "outputTokens": 1,
                    "maxPromptTokens": WINDOW_TOKENS,
                }
            },
        }
    )
    return "".join(json.dumps(event) + "\n" for event in events)


def rate_limit_line() -> str:
    return (
        json.dumps(
            {
                "type": "session.error",
                "data": {"errorType": "rate_limit", "errorCode": "rate_limited"},
            }
        )
        + "\n"
    )


@dataclass(kw_only=True)
class RecordingTaskQueue(FakeTaskQueue):
    completed: list[str] = field(default_factory=list[str])
    requeued: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def complete(self, task_id: str, agent: ActorId) -> None:
        self.completed.append(task_id)

    def requeue(self, task_id: str, reason: str) -> None:
        self.requeued.append((task_id, reason))


@dataclass(kw_only=True)
class StrictLeases(FakeLeases):
    """Refuses a second lease for the same agent, as the real leases do."""

    acquired_handles: list[str] = field(default_factory=list[str])

    def acquire(self, agent: ActorId, task: Task, handle_id: str) -> Lease:
        if agent in self.leases:
            raise ValueError(f"{agent} already holds a lease")
        else:
            self.acquired_handles.append(handle_id)
            return super().acquire(agent, task, handle_id)


@dataclass(kw_only=True)
class DiffingIndexer(FakeIndexer):
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
            cursor=cursor + 7,
            omitted_count=0,
            rendered=f"news since {cursor}",
        )


@dataclass(kw_only=True)
class FakeToolServer:
    claims: dict[tuple[ActorId, str], list[str]] = field(
        default_factory=dict[tuple[ActorId, str], list[str]]
    )

    def endpoint_for(self, agent: ActorId) -> ToolEndpoint:
        return ToolEndpoint(url="memory://tools/mcp", bearer_token=f"token-{agent}")

    def hook_config_for(self, agent: ActorId) -> HookConfig:
        return HookConfig(
            interrupt_check_url="memory://tools/hook/check",
            interrupt_ack_url="memory://tools/hook/ack",
        )

    def claims_written(self, agent: ActorId, task_id: str) -> tuple[str, ...]:
        return tuple(self.claims.get((agent, task_id), []))

    def serve(self) -> None:
        pass


@dataclass(kw_only=True)
class RecordingRegistry:
    registered: list[tuple[ActorId, Path, str | None]] = field(
        default_factory=list[tuple[ActorId, Path, str | None]]
    )

    def register_agent(
        self, agent: ActorId, sandbox_root: Path, task_id: str | None = None
    ) -> str:
        self.registered.append((agent, sandbox_root, task_id))
        return f"token-{agent}"


@dataclass(kw_only=True)
class TranscriptHarness:
    """ScriptedHarness that gives each session a transcript file, read as Claude Code's."""

    scripted: ScriptedHarness
    root: Path
    built: list[StartPack] = field(default_factory=list[StartPack])

    @property
    def supports_tool_hooks(self) -> bool:
        return True

    @property
    def supports_checkpoint(self) -> bool:
        return False

    def build(
        self,
        agent: ActorId,
        task: Task,
        start_pack: StartPack,
        tool_endpoint: ToolEndpoint,
        hook_config: HookConfig,
        workspace: Workspace,
    ) -> AgentSpec:
        self.built.append(start_pack)
        spec = self.scripted.build(
            agent, task, start_pack, tool_endpoint, hook_config, workspace
        )
        transcript = self.root / f"transcript-{len(self.built)}.jsonl"
        return spec.model_copy(
            update={"env": {**spec.env, TRANSCRIPT_VARIABLE: str(transcript)}}
        )

    def usage(self, transcript_path: Path) -> ContextUsage:
        return ContextUsage(
            used_tokens=latest_context(transcript_path).used_tokens,
            window_tokens=WINDOW_TOKENS,
        )

    def recent_tool_calls(self, transcript_path: Path, count: int) -> tuple[str, ...]:
        return tuple(tool_calls(transcript_path)[-count:])

    def rate_limit_retries(self, transcript_path: Path) -> int:
        return rate_limit_events(transcript_path)

    def export_state(self, handle: Handle) -> bytes:
        raise NotImplementedError

    def import_state(self, state_blob: bytes) -> AgentSpec:
        raise NotImplementedError


@dataclass(frozen=True, kw_only=True)
class MemorySessionFiles:
    executor: MemoryExecutor
    root: Path

    def sandbox_path(self, agent: ActorId, task_id: str) -> Path:
        return self.root / "sandboxes" / agent / task_id

    def transcript_path(self, handle: Handle) -> Path:
        spec = self.executor.processes[handle.handle_id].spec
        return Path(spec.env[TRANSCRIPT_VARIABLE])


@dataclass(frozen=True, kw_only=True)
class WorkerHarness:
    deps: WorkerDeps
    blackboard: StoreBlackboard
    executor: MemoryExecutor
    task_queue: RecordingTaskQueue
    leases: StrictLeases
    tool_server: FakeToolServer
    harness: TranscriptHarness
    registry: RecordingRegistry
    inbox: InterruptInbox
    clock: FakeClock
    stop: Event

    def write_claim(self, agent: ActorId, task: Task, number: int, body: str) -> str:
        """What a real agent does through repo_write: merge a claim and have it counted."""
        claim = Claim(
            id=f"c-{agent}-{number:05d}",
            type="insight",
            status="proposed",
            approach=task.approach,
            author=agent,
            evidence=(EvidenceRef(path="spec.md"),),
            body=body,
        )
        self.blackboard.write(
            agent, {f"claims/{agent}/{claim.id}.md": render_claim(claim).encode()}, "c"
        )
        self.tool_server.claims.setdefault((agent, task.id), []).append(claim.id)
        return claim.id


def make_worker_harness(
    tmp_path: Path,
    arm: ArmConfig,
    programs: dict[str, ScriptedProgram],
    default_program: str,
    params: Params | None = None,
) -> WorkerHarness:
    clock = FakeClock()
    effective_params = params or Params()
    blackboard = make_store_blackboard(clock, name="worker")
    blackboard.write(ActorId("human"), {"spec.md": b"# Spec\n\nGo faster."}, "spec")
    executor = MemoryExecutor(
        clock=clock,
        programs=programs,
        heartbeat_interval_seconds=effective_params.heartbeat_interval_seconds,
    )
    task_queue = RecordingTaskQueue()
    leases = StrictLeases(clock=clock)
    tool_server = FakeToolServer()
    harness = TranscriptHarness(
        scripted=ScriptedHarness(default_program=default_program), root=tmp_path
    )
    registry = RecordingRegistry()
    inbox = InterruptInbox()
    stop = Event()
    deps = WorkerDeps(
        task_queue=task_queue,
        leases=leases,
        executor=executor,
        sessions=MemorySessionFiles(executor=executor, root=tmp_path),
        harness=harness,
        tool_server=tool_server,
        registry=registry,
        context=ContextSources(
            blackboard=blackboard,
            indexer=DiffingIndexer(),
            arm=arm,
            params=effective_params,
        ),
        inbox=inbox,
        workspace=Workspace(
            target_repository="memory://target",
            target_ref="main",
            blackboard_clone_url="memory://blackboard",
        ),
        clock=clock,
        stop=stop,
    )
    return WorkerHarness(
        deps=deps,
        blackboard=blackboard,
        executor=executor,
        task_queue=task_queue,
        leases=leases,
        tool_server=tool_server,
        harness=harness,
        registry=registry,
        inbox=inbox,
        clock=clock,
        stop=stop,
    )
