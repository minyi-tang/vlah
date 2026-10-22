"""Conformance checks: every adapter of a port must pass the same checks (D61, docs/sdk.md).

Each check takes a fresh adapter instance and raises AssertionError on the first
broken rule. Checks that need time to pass take a CheckTimer, so in-memory adapters
can move a fake clock while real ones sleep.
"""

from dataclasses import dataclass, field
from typing import Protocol

from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.ports.errors import (
    AppendOnlyViolationError,
    NotSupportedError,
    PushRejectedError,
    ReadPermissionError,
)
from vlah.ports.executor import AgentSpec, Executor, StartPack
from vlah.ports.harness import Harness, HookConfig, ToolEndpoint, Workspace
from vlah.ports.signal_source import CandidateRef, SignalSource
from vlah.ports.store import (
    KeyValueStore,
    MergeResult,
    ObjectStore,
    VectorIndex,
    VersionedStore,
)

CHECK_AUTHOR = ActorId("worker-01")


class CheckTimer(Protocol):
    def wait_seconds(self, seconds: float) -> None:
        """Let this much time pass for the adapter under test."""
        ...


@dataclass(kw_only=True)
class RecordingHeartbeatSink:
    beats: list[tuple[ActorId, tuple[str, ...], float]] = field(
        default_factory=list[tuple[ActorId, tuple[str, ...], float]]
    )

    def beat(
        self, agent: ActorId, files_touched: tuple[str, ...], timestamp_seconds: float
    ) -> None:
        self.beats.append((agent, files_touched, timestamp_seconds))


@dataclass(kw_only=True)
class RecordingMergeListener:
    merges: list[MergeResult] = field(default_factory=list[MergeResult])

    def __call__(self, merge_result: MergeResult) -> None:
        self.merges.append(merge_result)


def check_versioned_store(store: VersionedStore, push_token: str) -> None:
    """Push credential, append-only merges, increasing sequence numbers, history, hooks."""
    try:
        store.commit(
            "w/check", {"claims/x.md": b"x"}, CHECK_AUTHOR, "no token", "wrong"
        )
    except PushRejectedError:
        pass
    else:
        raise AssertionError("a commit without the push token must be rejected")

    listener = RecordingMergeListener()
    subscription_id = store.subscribe(listener)
    start_sequence = store.head_sequence()
    store.commit(
        "w/check",
        {
            "events/worker-01/00002.json": b"{}",
            "events/worker-01/00001.json": b"{}",
            "claims/worker-01/c.md": b"claim",
        },
        CHECK_AUTHOR,
        "first",
        push_token,
    )
    (first_merge,) = store.merge("w/check")
    assert first_merge.event_sequence_numbers == {
        "events/worker-01/00001.json": start_sequence + 1,
        "events/worker-01/00002.json": start_sequence + 2,
    }, "event files get the next sequence numbers in path order"
    assert store.read(f"events/_seq/{start_sequence + 1}", None) == (
        b"events/worker-01/00001.json"
    ), "each sequence number has a pointer file naming its event"

    store.commit(
        "w/check",
        {"events/worker-01/00003.json": b"{}"},
        CHECK_AUTHOR,
        "second",
        push_token,
    )
    (second_merge,) = store.merge("w/check")
    assert second_merge.event_sequence_numbers == {
        "events/worker-01/00003.json": start_sequence + 3
    }, "sequence numbers keep increasing across merges"
    assert store.head_sequence() == start_sequence + 3
    assert not store.list_paths(
        "events/worker-01/00003", start_sequence + 2
    ), "reading at an earlier sequence number shows the state of that merge"
    assert store.exists("claims/worker-01/c.md")

    store.commit(
        "w/check", {"claims/worker-01/c.md": b"changed"}, CHECK_AUTHOR, "x", push_token
    )
    try:
        store.merge("w/check")
    except AppendOnlyViolationError:
        pass
    else:
        raise AssertionError("overwriting an existing file must be rejected")
    assert store.read("claims/worker-01/c.md", None) == b"claim"

    for clashing_files in (
        {"claims/worker-01/c.md/inner": b"x"},
        {"events/worker-01": b"x"},
        {"claims/worker-01/d": b"x", "claims/worker-01/d/inner": b"x"},
    ):
        store.commit("w/check", clashing_files, CHECK_AUTHOR, "clash", push_token)
        try:
            store.merge("w/check")
        except AppendOnlyViolationError:
            pass
        else:
            raise AssertionError(
                f"a file and a directory cannot share a name: {sorted(clashing_files)}"
            )
    assert store.read("claims/worker-01/c.md", None) == b"claim"
    assert store.list_paths("events/worker-01/", None) == (
        "events/worker-01/00001.json",
        "events/worker-01/00002.json",
        "events/worker-01/00003.json",
    ), "a rejected clash leaves the tree as it was"

    assert [merge.commit_id for merge in listener.merges] == [
        first_merge.commit_id,
        second_merge.commit_id,
    ], "listeners hear every merge, in order"
    store.unsubscribe(subscription_id)


def check_key_value_store(store: KeyValueStore, timer: CheckTimer) -> None:
    """TTL, compare-and-set, scans, counters and sorted sets."""
    store.set("check/a", b"1", None)
    assert store.get("check/a") == b"1"
    store.set("check/ttl", b"x", 10.0)
    timer.wait_seconds(11.0)
    assert store.get("check/ttl") is None, "values expire after their TTL"
    assert store.compare_and_set("check/new", None, b"v1", None), "absent matches None"
    assert not store.compare_and_set("check/new", b"other", b"v2", None)
    assert store.compare_and_set("check/new", b"v1", b"v2", None)
    assert store.get("check/new") == b"v2"
    assert store.scan("check/") == ("check/a", "check/new")
    assert store.delete("check/a")
    assert not store.delete("check/a")
    assert store.increment("check/count", 2) == 2
    assert store.increment("check/count", 3) == 5
    store.sorted_add("check/queue", "b", 1.0)
    store.sorted_add("check/queue", "a", 1.0)
    store.sorted_add("check/queue", "c", 0.5)
    assert store.sorted_range("check/queue") == (("c", 0.5), ("a", 1.0), ("b", 1.0))
    assert store.sorted_remove("check/queue", "c")
    assert not store.sorted_remove(
        "check/queue", "c"
    ), "only one caller removes a member"


def check_object_store(
    store: ObjectStore, worker: ActorId, private_reader: ActorId
) -> None:
    """Pointers, archive-only writes, and private data hidden from workers (D33, D63)."""
    public_pointer = store.put("traces/worker-01/t-1", b"trace", False)
    private_pointer = store.put("judge/data", b"hidden", True)
    assert store.exists(public_pointer)
    assert store.get(public_pointer, worker) == b"trace"
    assert store.get(private_pointer, private_reader) == b"hidden"
    try:
        store.get(private_pointer, worker)
    except ReadPermissionError:
        pass
    else:
        raise AssertionError("a worker must not read private data")
    try:
        store.put("traces/worker-01/t-1", b"rewritten", False)
    except AppendOnlyViolationError:
        pass
    else:
        raise AssertionError("an archived object must not be rewritten")


def check_vector_index(index: VectorIndex) -> None:
    index.upsert("a", (1.0, 0.0), {"kind": "claim"})
    index.upsert("b", (0.0, 1.0), {"kind": "claim"})
    index.upsert("c", (0.9, 0.1), {"kind": "trace"})
    hits = index.query((1.0, 0.0), 2, None)
    assert [hit.item_id for hit in hits] == ["a", "c"], "most similar first"
    assert abs(hits[0].similarity - 1.0) < 1e-9
    filtered = index.query((1.0, 0.0), 5, {"kind": "claim"})
    assert [hit.item_id for hit in filtered] == ["a", "b"]
    assert index.delete("a")
    assert not index.delete("a")


@dataclass(frozen=True, kw_only=True)
class ExecutorCheckInputs:
    """long_running_spec must run silently for at least ten heartbeat intervals;
    quick_spec must exit with code 0 within a few."""

    long_running_spec: AgentSpec
    quick_spec: AgentSpec
    task: Task
    start_pack: StartPack
    heartbeat_interval_seconds: float


def check_executor(
    executor: Executor, inputs: ExecutorCheckInputs, timer: CheckTimer
) -> None:
    """Heartbeats come from the executor, even when the agent is silent (D32, D60)."""
    sink = RecordingHeartbeatSink()
    long_handle = executor.start(
        inputs.long_running_spec, inputs.task, inputs.start_pack, sink
    )
    assert executor.is_alive(long_handle)
    timer.wait_seconds(3.5 * inputs.heartbeat_interval_seconds)
    beats_for_agent = [beat for beat in sink.beats if beat[0] == long_handle.agent]
    assert (
        len(beats_for_agent) >= 3
    ), f"expected at least 3 heartbeats from a silent agent, got {len(beats_for_agent)}"
    assert executor.is_alive(long_handle)
    executor.kill(long_handle)
    long_exit = executor.wait(long_handle, inputs.heartbeat_interval_seconds)
    assert not executor.is_alive(long_handle)
    assert long_exit is not None and long_exit.was_killed

    quick_handle = executor.start(
        inputs.quick_spec, inputs.task, inputs.start_pack, sink
    )
    quick_exit = executor.wait(quick_handle, 5 * inputs.heartbeat_interval_seconds)
    assert quick_exit is not None and quick_exit.return_code == 0
    try:
        executor.checkpoint(quick_handle)
    except NotSupportedError:
        pass
    else:
        pass


def check_signal_source(
    source: SignalSource, candidate: CandidateRef, actor: ActorId, timer: CheckTimer
) -> None:
    """Results stay hidden until the declared latency has passed, and always carry n.

    A first submission is a run, never a cache hit; only a repeat may be one.
    """
    job_id = source.submit(candidate, actor, "baseline@0", "dev")
    latency_seconds = source.declaration.latency_seconds
    if latency_seconds > 0:
        assert source.poll(job_id) is None, "no result before the declared latency"
    else:
        pass
    timer.wait_seconds(latency_seconds + 1.0)
    result = source.poll(job_id)
    assert result is not None, "a result is visible after the declared latency"
    assert result.n >= 1
    assert not result.is_cached, "a first submission is a run, not a cache hit"


@dataclass(frozen=True, kw_only=True)
class HarnessCheckInputs:
    agent: ActorId
    task: Task
    start_pack: StartPack
    tool_endpoint: ToolEndpoint
    hook_config: HookConfig
    workspace: Workspace


def check_harness(harness: Harness, inputs: HarnessCheckInputs) -> None:
    spec = harness.build(
        inputs.agent,
        inputs.task,
        inputs.start_pack,
        inputs.tool_endpoint,
        inputs.hook_config,
        inputs.workspace,
    )
    assert spec.agent == inputs.agent
    assert spec.command, "an agent spec needs a command"
    assert spec.target_repository == inputs.workspace.target_repository
    assert spec.blackboard_clone_url == inputs.workspace.blackboard_clone_url
    if not harness.supports_checkpoint:
        try:
            harness.import_state(b"")
        except NotSupportedError:
            pass
        else:
            raise AssertionError(
                "import_state must raise NotSupportedError without checkpoints"
            )
    else:
        pass
