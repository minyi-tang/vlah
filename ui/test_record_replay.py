"""The UI fixture must come from executed core behavior, not a branching schedule."""

import json
from collections.abc import Iterator
from pathlib import Path
from random import Random

from tests.fixtures.fakes import FakeLeases
from tests.fixtures.herd import make_store_blackboard
from tests.fixtures.runtime import MemoryAssembly, WaitingRunner, toy_problem
from ui.live import RecordingAssembly, start_recorded
from ui.observed_executor import ObservedExecutor
from ui.record_replay import record
from ui.replay_recording import TraceRecorder
from vlah.adapters.memory.executor import MemoryExecutor, ScriptedSession
from vlah.adapters.memory.model import MemoryModel
from vlah.adapters.memory.store import MemoryVectorIndex
from vlah.core.indexer.indexer import StoreIndexer
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task
from vlah.ports.executor import AgentSpec, StartPack
from vlah.runtime.herd import build_herd


def test_recording_is_deterministic_and_contains_real_feedback(tmp_path: Path) -> None:
    first = record(tmp_path / "first")
    second = record(tmp_path / "second")
    assert first == second
    recording = json.loads(first)
    workers = recording["workers"]
    assert len(workers) == 101
    assert len(set(workers)) == 101
    nodes = {node["id"]: node for node in recording["nodes"]}
    events = recording["events"]
    kinds = {event["kind"] for event in events}
    assert {
        "result_ready",
        "baseline_advanced",
        "integration_failed",
        "interaction_found",
    } <= kinds
    assert "spawn" not in kinds
    assert all(
        edge["source"] in nodes and edge["target"] in nodes
        for edge in recording["edges"]
    )
    assert all(session["end"] is not None for session in recording["sessions"])
    assert {session["actor"] for session in recording["sessions"]} == set(workers)
    followups = [
        session
        for session in recording["sessions"]
        if session["kind"] == "result_followup"
    ]
    assert followups
    assert all(
        any(
            edge["target"] == session["task"] and edge["relation"] == "task reference"
            for edge in recording["edges"]
        )
        for session in followups
    )
    assert any(session["kind"] == "rebase_compose" for session in recording["sessions"])
    assert any(
        sum(
            edge["target"] == identity and edge["relation"] == "candidate parent"
            for edge in recording["edges"]
        )
        > 1
        for identity in nodes
    )


def test_live_observer_only_reads_and_publishes_observed_changes(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    blackboard = make_store_blackboard(clock)
    indexer = StoreIndexer(
        blackboard=blackboard,
        vectors=MemoryVectorIndex(),
        model=MemoryModel(),
        params=Params(),
        clock=clock,
        top_level=None,
    )
    blackboard.subscribe(indexer.apply)
    blackboard.write(ActorId("human"), {"spec.md": b"# Public problem\n"}, "problem")
    baseline = BaselineRecord(k=0, parent=None, candidates=(), created_ts=0)
    blackboard.write(
        ActorId("integrator"),
        {"baseline/0.json": baseline.model_dump_json().encode()},
        "initial baseline",
    )
    before = blackboard.list_paths("", None)
    head = blackboard.head_sequence()
    output = tmp_path / "recording.json"
    observer = TraceRecorder(blackboard, indexer, clock)
    observer.start(output, ("worker-01", "worker-compose"), "worker-compose")
    assert blackboard.list_paths("", None) == before
    assert blackboard.head_sequence() == head
    clock.advance(2)
    claim = Claim(
        id="c-worker-01-00001",
        type="question",
        status="open",
        approach="a-01",
        author=ActorId("worker-01"),
        evidence=(EvidenceRef(path="spec.md"),),
        body="A public question.",
    )
    blackboard.write(
        claim.author,
        {f"claims/{claim.author}/{claim.id}.md": render_claim(claim).encode()},
        "question",
    )
    published = json.loads(output.read_text())
    assert any(
        node["id"] == claim.id and node["at"] == 2 for node in published["nodes"]
    )
    assert (
        published["sessions"] == []
    ), "Claims alone must not manufacture session liveness."
    observer.stop()
    stopped = output.read_bytes()
    clock.advance(3)
    blackboard.write(
        ActorId("worker-01"), {"traces/worker-01/public.txt": b"Not exported"}, "trace"
    )
    assert output.read_bytes() == stopped


def waiting_work(session: ScriptedSession) -> Iterator[float]:
    yield 5


def test_executor_observation_preserves_worker_and_task_across_sessions(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    blackboard = make_store_blackboard(clock)
    indexer = StoreIndexer(
        blackboard=blackboard,
        vectors=MemoryVectorIndex(),
        model=MemoryModel(),
        params=Params(),
        clock=clock,
        top_level=None,
    )
    recorder = TraceRecorder(blackboard, indexer, clock)
    executor = MemoryExecutor(
        clock=clock,
        programs={"work": waiting_work},
        heartbeat_interval_seconds=1,
        sandbox_root=tmp_path,
    )
    observed = ObservedExecutor(executor, recorder)
    actor = ActorId("worker-01")
    task = Task(
        id="t-00001",
        approach="a-01",
        kind="advance",
        goal="Continue the same task.",
        created_by="queue",
    )
    spec = AgentSpec(
        agent=actor,
        command=("scripted", "work"),
        target_repository="memory://target",
        target_ref="baseline@0",
        blackboard_clone_url="memory://board",
    )
    first = observed.start(spec, task, StartPack(files={}), FakeLeases(clock=clock))
    executor.advance(6)
    assert observed.wait(first, 0) is not None
    second = observed.start(spec, task, StartPack(files={}), FakeLeases(clock=clock))
    assert len(recorder.sessions) == 2
    assert len(recorder.nodes) == 1
    assert recorder.nodes["task:t-00001"].states[-1].status == "running"
    observed.kill(second)
    assert recorder.sessions[-1].end is not None


def test_live_run_wrapper_attaches_before_workers_and_closes_after_runtime(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    problem = toy_problem(tmp_path, worker_count=2)
    assembly = RecordingAssembly(
        MemoryAssembly(
            clock=clock,
            root=tmp_path,
            use_real_eval=True,
            use_real_integrator=True,
        )
    )
    herd = build_herd(
        problem,
        "S2",
        assembly,
        "observed-run",
        root=tmp_path,
        worker_count=2,
        duration_seconds=60,
        clock=clock,
        random=Random(0),
        runner=WaitingRunner(),
    )
    output = tmp_path / "observed.json"
    run = start_recorded(herd, output)
    try:
        initial = json.loads(output.read_text())
        assert initial["name"] == "observed-run"
        assert len(initial["workers"]) == 3
        assert any(node["id"] == "baseline@0" for node in initial["nodes"])
        assert run.status().worker_count == 2
    finally:
        run.stop()
    assert run.wait(1)
    assert not run.status().is_running
    assert run.recorder.subscription is None
    assert run.recorder.output_path is None
    assert run.report().deliverable.k == 0
    completed = json.loads(output.read_text())
    assert completed["run_states"][-1]["status"]["is_running"] is False
    assert completed["run_states"][-1]["status"]["worker_count"] == 2
    assert completed["report"]["deliverable"]["k"] == run.report().deliverable.k
    assert completed["report"]["metrics"]["holdout_score"] is None
    identities = {node["id"] for node in completed["nodes"]}
    assert all(
        edge["source"] in identities and edge["target"] in identities
        for edge in completed["edges"]
    )
