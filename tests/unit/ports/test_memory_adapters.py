"""Every in-memory adapter passes the same conformance check a real adapter must pass."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from vlah.adapters.memory.executor import (
    MemoryExecutor,
    ScriptedHarness,
    ScriptedProgram,
    ScriptedSession,
)
from vlah.adapters.memory.signal_source import FakeSlowSource
from vlah.adapters.memory.store import (
    MemoryKeyValueStore,
    MemoryObjectStore,
    MemoryVectorIndex,
    MemoryVersionedStore,
)
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import SourceBudget, SourceDecl
from vlah.core.types.tasks import Task
from vlah.ports.executor import AgentSpec, StartPack
from vlah.ports.harness import HookConfig, ToolEndpoint, Workspace
from vlah.ports.signal_source import CandidateRef
from vlah.testing.conformance import (
    ExecutorCheckInputs,
    HarnessCheckInputs,
    check_executor,
    check_harness,
    check_key_value_store,
    check_object_store,
    check_signal_source,
    check_vector_index,
    check_versioned_store,
)

HEARTBEAT_INTERVAL_SECONDS = 30.0
TASK = Task(
    id="t-00001",
    approach="a-01",
    kind="advance",
    goal="Advance the problem",
    created_by="queue",
)
START_PACK = StartPack(files={"CONTEXT.md": "spec"})
WORKSPACE = Workspace(
    target_repository="memory://target",
    target_ref="baseline@0",
    blackboard_clone_url="memory://blackboard?read_only=1",
)


@dataclass(frozen=True, kw_only=True)
class ClockTimer:
    clock: FakeClock

    def wait_seconds(self, seconds: float) -> None:
        self.clock.advance(seconds)


@dataclass(frozen=True, kw_only=True)
class ExecutorTimer:
    executor: MemoryExecutor

    def wait_seconds(self, seconds: float) -> None:
        self.executor.advance(seconds)


def sleep_for_a_day(session: ScriptedSession) -> Iterator[float]:
    session.files_touched.add("policy.py")
    yield 86400.0


def exit_at_once(session: ScriptedSession) -> Iterator[float]:
    yield from ()


def agent_spec(program_name: str) -> AgentSpec:
    return AgentSpec(
        agent=ActorId("worker-01"),
        command=("scripted", program_name),
        target_repository=WORKSPACE.target_repository,
        target_ref=WORKSPACE.target_ref,
        blackboard_clone_url=WORKSPACE.blackboard_clone_url,
    )


def test_versioned_store() -> None:
    check_versioned_store(
        MemoryVersionedStore(name="blackboard", push_token="secret"), "secret"
    )


def test_key_value_store() -> None:
    clock = FakeClock()
    check_key_value_store(
        MemoryKeyValueStore(clock=clock, namespace="run-1"), ClockTimer(clock=clock)
    )


def test_object_store_hides_private_data_from_workers() -> None:
    check_object_store(
        MemoryObjectStore(), ActorId("worker-01"), ActorId("eval_service")
    )


def test_vector_index() -> None:
    check_vector_index(MemoryVectorIndex())


def test_executor_heartbeats_while_the_agent_is_silent() -> None:
    programs: dict[str, ScriptedProgram] = {
        "sleep": sleep_for_a_day,
        "quick": exit_at_once,
    }
    executor = MemoryExecutor(
        clock=FakeClock(),
        programs=programs,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )
    check_executor(
        executor,
        ExecutorCheckInputs(
            long_running_spec=agent_spec("sleep"),
            quick_spec=agent_spec("quick"),
            task=TASK,
            start_pack=START_PACK,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        ),
        ExecutorTimer(executor=executor),
    )


def test_signal_source_hides_results_until_the_latency_passes(tmp_path: Path) -> None:
    clock = FakeClock()
    declaration = SourceDecl(
        id="bench_full",
        mode="pull",
        latency="10m",
        budget=SourceBudget(per_day=48, allocated_by="human"),
        cost_machine_hours=1.0,
        trust=0.9,
        readable_by=("critic",),
    )
    candidate = CandidateRef(
        candidate_hash="9f3a",
        payload_directory=tmp_path,
        manifest=CandidateManifest(
            kind="patch",
            target="baseline@0",
            approach="a-01",
            author=ActorId("worker-01"),
            created_at=0.0,
        ),
    )
    check_signal_source(
        FakeSlowSource(clock=clock, declaration=declaration),
        candidate,
        ActorId("worker-01"),
        ClockTimer(clock=clock),
    )


def test_scripted_harness() -> None:
    check_harness(
        ScriptedHarness(default_program="quick"),
        HarnessCheckInputs(
            agent=ActorId("worker-01"),
            task=TASK,
            start_pack=START_PACK,
            tool_endpoint=ToolEndpoint(url="memory://tools", bearer_token="token"),
            hook_config=HookConfig(
                interrupt_check_url="memory://tools/hook/check",
                interrupt_ack_url="memory://tools/hook/ack",
            ),
            workspace=WORKSPACE,
        ),
    )
