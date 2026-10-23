"""The herd's tools: write gates, read scope by arm, recorder events, sync-point diffs (v2.2 section 7)."""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.fixtures.tools import ToolsHarness, make_tools_harness
from vlah.arms.config import PRESETS, ArmName
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task
from vlah.tools.herd_tools import HerdTools, ToolReply

pytestmark = pytest.mark.tools

WORKER = ActorId("worker-01")
OTHER = ActorId("worker-02")
TASK = Task(
    id="t-00001", approach="a-01", kind="advance", goal="Advance", created_by="queue"
)


def claim_text(
    author: ActorId, number: int, claim_type: str, evidence: EvidenceRef
) -> tuple[str, str]:
    claim = Claim.model_validate(
        {
            "id": f"c-{author}-{number:05d}",
            "type": claim_type,
            "status": "proposed",
            "approach": "a-01",
            "author": author,
            "evidence": [evidence.model_dump(exclude_none=True)],
            "body": f"A {claim_type}.",
        }
    )
    return f"claims/{author}/{claim.id}.md", render_claim(claim)


def test_writes_outside_the_callers_prefix_never_reach_the_blackboard(
    tmp_path: Path,
) -> None:
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path)
    path, text = claim_text(OTHER, 1, "insight", EvidenceRef(path="spec.md"))
    reply = harness.tools.repo_write(WORKER, {path: text})
    assert "own prefix" in str(reply.result["error"])
    assert harness.blackboard.stats().merge_count == 0


def test_a_claim_joins_the_lease_and_the_recorder_sees_it(tmp_path: Path) -> None:
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path)
    harness.leases.acquire(WORKER, TASK, "handle-1")
    harness.blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    path, text = claim_text(WORKER, 1, "insight", EvidenceRef(path="spec.md"))
    reply = harness.tools.repo_write(WORKER, {path: text})
    assert "error" not in reply.result
    lease = harness.leases.get(WORKER)
    assert lease is not None and "c-worker-01-00001" in lease.target_claims
    assert harness.tools.claims_written(WORKER, TASK.id) == ("c-worker-01-00001",)
    (event,) = harness.recorder.events
    assert (event.type, event.data["kind"], event.data["id"]) == (
        "blackboard_write",
        "insight",
        "c-worker-01-00001",
    )


def test_a_dead_end_must_name_the_candidate_it_tested(tmp_path: Path) -> None:
    """The scorecard counts dead-end re-hits by candidate hash (TB section 11.5)."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path)
    harness.blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    vague_path, vague = claim_text(WORKER, 1, "dead_end", EvidenceRef(path="spec.md"))
    assert "must cite the candidate" in str(
        harness.tools.repo_write(WORKER, {vague_path: vague}).result["error"]
    )
    harness.blackboard.write(
        WORKER,
        {
            "candidates/9f3a/manifest.yaml": b"kind: patch\ntarget: baseline@0\napproach: a-01\nauthor: worker-01\ncreated_at: 0\n"
        },
        "c",
    )
    cited_path, cited = claim_text(
        WORKER, 2, "dead_end", EvidenceRef(candidate="candidates/9f3a/result.json")
    )
    assert "error" not in harness.tools.repo_write(WORKER, {cited_path: cited}).result
    assert harness.recorder.events[-1].data["hash"] == "9f3a"


@pytest.mark.parametrize(
    ("arm", "is_readable"), [("S2", True), ("S1", False), ("B1", False)]
)
def test_other_workers_claims_are_readable_only_with_sharing(
    tmp_path: Path, arm: ArmName, is_readable: bool
) -> None:
    harness = make_tools_harness(tmp_path, PRESETS[arm])
    harness.tools.register_agent(WORKER, tmp_path)
    harness.blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    path, text = claim_text(OTHER, 1, "insight", EvidenceRef(path="spec.md"))
    harness.blackboard.write(OTHER, {path: text.encode()}, "other")
    reply = harness.tools.repo_read(WORKER, path)
    assert ("error" not in reply.result) is is_readable
    assert "error" not in harness.tools.repo_read(WORKER, "spec.md").result


def test_repeated_failures_put_the_diff_on_every_reply_until_a_success(
    tmp_path: Path,
) -> None:
    """Consecutive failures are the strongest sign someone else already did it (D19)."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path)
    (tmp_path / "candidate").mkdir()
    harness.eval_service.statuses = [
        "gate_failed",
        "dedup_hit",
        "gate_failed",
        "queued",
    ]

    def ask() -> ToolReply:
        return harness.tools.eval_request(
            WORKER, "candidate", {"slow": 1.0}, "why", (), None, None
        )

    replies = [ask() for _ in range(3)]
    assert all(
        reply.diff is not None for reply in replies
    ), "every eval_request is a sync point"
    assert ["related" in reply.result for reply in replies] == [
        False,
        False,
        True,
    ], "D19 forces a search"
    assert (
        harness.tools.search(WORKER, "x", 5).diff is not None
    ), "three failures: refresh everywhere"
    ask()
    assert (
        harness.tools.search(WORKER, "x", 5).diff is None
    ), "a success ends the refresh"


def test_a_pending_interrupt_rides_on_the_reply_and_the_call_still_runs(
    tmp_path: Path,
) -> None:
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path)
    harness.blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    harness.router.pending[WORKER] = Interrupt(
        id="i-1",
        agent=WORKER,
        kind="hard",
        rule=2,
        reason="your target was refuted",
        written_ts=0.0,
    )
    reply = harness.tools.repo_read(WORKER, "spec.md")
    assert reply.interrupt is not None and reply.interrupt.id == "i-1"
    assert reply.result["content"] == "spec"


@pytest.mark.parametrize("is_compose", [False, True])
def test_only_the_compose_tool_server_sends_compose_requests(
    tmp_path: Path, is_compose: bool
) -> None:
    """The compose slot's requests reached the eval service as worker requests, so
    the arm's dedup and confirmation applied to them (#56 R5, D74)."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"], is_compose=is_compose)
    harness.tools.register_agent(WORKER, tmp_path)
    (tmp_path / "candidate").mkdir()
    harness.tools.eval_request(
        WORKER, "candidate", {"slow": 1.0}, "composed", (), None, None
    )
    (request,) = harness.eval_service.requests
    assert request.kind == ("compose" if is_compose else "worker")


def test_candidate_paths_cannot_leave_the_sandbox(tmp_path: Path) -> None:
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    harness.tools.register_agent(WORKER, sandbox)
    reply = harness.tools.eval_request(
        WORKER, "../", {"slow": 1.0}, "why", (), None, None
    )
    assert "not a directory in your sandbox" in str(reply.result["error"])
    assert harness.eval_service.requests == []


def test_concurrent_calls_keep_the_books_and_never_wait_on_a_gate(
    tmp_path: Path,
) -> None:
    """The MCP SDK runs tool calls on worker threads; all requests meet inside the eval service."""
    calls = 6
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path)
    harness.blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    (tmp_path / "candidate").mkdir()
    harness.eval_service.statuses = ["gate_failed"] * calls
    harness.eval_service.barrier = threading.Barrier(calls, timeout=10)
    with ThreadPoolExecutor(max_workers=calls * 2) as pool:
        requests = [
            pool.submit(
                harness.tools.eval_request,
                WORKER,
                "candidate",
                {"slow": 1.0},
                "why",
                (),
                None,
                None,
            )
            for _ in range(calls)
        ]
        surprises = [
            pool.submit(
                harness.tools.surprise, WORKER, f"odd {number}", "spec.md", "a-01"
            )
            for number in range(calls)
        ]
        evaluated = [future.result().result for future in requests]
        written = [future.result().result for future in surprises]
    assert [result["status"] for result in evaluated] == ["gate_failed"] * calls
    assert all("written" in result for result in written), written
    assert harness.tools.state(WORKER).consecutive_failures == calls
    claim_paths = harness.blackboard.list_paths(f"claims/{WORKER}/", None)
    assert len(claim_paths) == calls


MANIFEST = b"kind: patch\ntarget: baseline@0\napproach: a-01\nauthor: worker-01\ncreated_at: 0\n"


def test_tokens_are_secrets_not_draws_from_the_seeded_random(tmp_path: Path) -> None:
    """Two herds seeded alike must not issue the same credentials."""
    first = make_tools_harness(tmp_path / "a", PRESETS["S2"])
    second = make_tools_harness(tmp_path / "b", PRESETS["S2"])
    token = first.tools.register_agent(WORKER, tmp_path)
    assert token != second.tools.register_agent(WORKER, tmp_path)
    assert len(token) == 32
    assert first.tools.register_agent(WORKER, tmp_path) == token, "stable per agent"


def test_a_malformed_claim_comes_back_as_an_error_with_the_interrupt(
    tmp_path: Path,
) -> None:
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path)
    harness.router.pending[WORKER] = Interrupt(
        id="i-1", agent=WORKER, kind="hard", rule=2, reason="r", written_ts=0.0
    )
    reply = harness.tools.repo_write(
        WORKER, {"claims/worker-01/c-worker-01-00001.md": "---\nid: oops\n---\n"}
    )
    assert "not a valid claim" in str(reply.result["error"])
    assert reply.interrupt is not None and reply.interrupt.id == "i-1"
    notes = harness.tools.repo_write(WORKER, {"traces/worker-01/notes.txt": "plain"})
    assert notes.result["written"] == ["traces/worker-01/notes.txt"]


def test_a_symbolic_link_in_the_candidate_is_refused(tmp_path: Path) -> None:
    """Following it would copy any host file onto the shared blackboard (D63)."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    sandbox = tmp_path / "sandbox"
    (sandbox / "candidate").mkdir(parents=True)
    (tmp_path / "herd.yaml").write_text("secret: true\n")
    (sandbox / "candidate" / "leak.yaml").symlink_to(tmp_path / "herd.yaml")
    harness.tools.register_agent(WORKER, sandbox)
    reply = harness.tools.eval_request(
        WORKER, "candidate", {"slow": 1.0}, "why", (), None, None
    )
    assert "symbolic link (leak.yaml)" in str(reply.result["error"])
    assert harness.eval_service.requests == []


def test_a_dead_end_may_cite_the_result_claim_instead_of_the_candidate(
    tmp_path: Path,
) -> None:
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path)
    harness.blackboard.write(WORKER, {"candidates/9f3a/manifest.yaml": MANIFEST}, "c")
    result_path, result_text = claim_text(
        ActorId("eval_service"),
        1,
        "result",
        EvidenceRef(candidate="candidates/9f3a/result.json"),
    )
    harness.blackboard.write(
        ActorId("eval_service"), {result_path: result_text.encode()}, "result"
    )
    path, text = claim_text(
        WORKER, 1, "dead_end", EvidenceRef(claim="c-eval_service-00001")
    )
    assert "error" not in harness.tools.repo_write(WORKER, {path: text}).result
    assert harness.recorder.events[-1].data["hash"] == "9f3a"


def test_a_recorder_outage_does_not_fail_the_tools(tmp_path: Path) -> None:
    """The write has merged by the time the recorder is called; it must not look failed."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path)
    harness.blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    harness.recorder.is_failing = True
    path, text = claim_text(WORKER, 1, "insight", EvidenceRef(path="spec.md"))
    assert harness.tools.repo_write(WORKER, {path: text}).result["written"] == [path]
    assert harness.tools.repo_read(WORKER, "spec.md").result["content"] == "spec"


def test_claims_before_the_lease_count_for_the_registered_task(tmp_path: Path) -> None:
    """The worker loop registers the task before the session starts and the lease exists."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path, TASK.id)
    harness.blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    path, text = claim_text(WORKER, 1, "insight", EvidenceRef(path="spec.md"))
    harness.tools.repo_write(WORKER, {path: text})
    assert harness.tools.claims_written(WORKER, TASK.id) == ("c-worker-01-00001",)


def test_two_agents_write_at_once(tmp_path: Path) -> None:
    """Both writes reach the recorder together: no lock is held across another agent's I/O."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    for agent in (WORKER, OTHER):
        harness.tools.register_agent(agent, tmp_path)
    harness.recorder.barrier = threading.Barrier(2, timeout=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(
            pool.map(
                lambda agent: harness.tools.repo_write(
                    agent,
                    dict(
                        [claim_text(agent, 1, "insight", EvidenceRef(path="spec.md"))]
                    ),
                ),
                (WORKER, OTHER),
            )
        )
    assert all("written" in reply.result for reply in replies)
    assert len(harness.recorder.events) == 2


def test_a_rebuilt_tool_server_still_reports_the_tasks_claims(tmp_path: Path) -> None:
    """A restart forgot every claim, so the worker loop requeued a task that had one."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path, TASK.id)
    harness.blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    followup = TASK.model_copy(
        update={"refs": ("c-eval_service-00001", "candidates/9f3a/result.json")}
    )
    harness.leases.acquire(WORKER, followup, "handle-1")
    path, text = claim_text(WORKER, 1, "insight", EvidenceRef(path="spec.md"))
    assert "error" not in harness.tools.repo_write(WORKER, {path: text}).result
    rebuilt = HerdTools(
        blackboard=harness.blackboard,
        objects=harness.objects,
        indexer=harness.tools.indexer,
        eval_service=harness.eval_service,
        leases=harness.leases,
        router=harness.router,
        recorder=harness.recorder,
        arm=PRESETS["S2"],
        params=Params(),
        clock=harness.clock,
        run_id="r-tools",
    )
    rebuilt.register_agent(WORKER, tmp_path, TASK.id)
    assert rebuilt.claims_written(WORKER, TASK.id) == ("c-worker-01-00001",)
    assert harness.tools.claims_written(WORKER, TASK.id) == (
        "c-worker-01-00001",
    ), "the lease and the booking name the same claim once"
    harness.leases.release(WORKER)
    assert rebuilt.claims_written(WORKER, TASK.id) == ()


def surprise_with(harness: ToolsHarness, pointer: str) -> ToolReply:
    return harness.tools.surprise(WORKER, "odd", pointer, "a-01")


def write_with(harness: ToolsHarness, pointer: str) -> ToolReply:
    path, text = claim_text(WORKER, 1, "insight", EvidenceRef(trace=pointer))
    return harness.tools.repo_write(WORKER, {path: text})


@pytest.mark.parametrize("cite", [surprise_with, write_with])
def test_private_evidence_is_refused_without_looking_it_up(
    tmp_path: Path, cite: Callable[[ToolsHarness, str], ToolReply]
) -> None:
    """The blackboard's receipt told a worker whether a private key existed (D63)."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path)
    existing = harness.objects.put("eval/holdout.json", b"secret", is_private=True)
    missing = "private://eval/nothing.json"
    replies = {pointer: cite(harness, pointer) for pointer in (existing, missing)}
    errors = {
        pointer: str(reply.result["error"]).replace(pointer, "<pointer>")
        for pointer, reply in replies.items()
    }
    assert "is private" in errors[existing]
    assert errors[existing] == errors[missing], "the reply must not depend on existence"
    assert harness.blackboard.stats().merge_count == 0
    assert harness.recorder.events == []


def test_a_new_agents_cursor_starts_at_the_indexers_head_not_the_blackboards(
    tmp_path: Path,
) -> None:
    """A merge numbered but not yet folded into the index must still reach the agent's
    first diff, so the cursor starts where the indexer is (D17)."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    worker = ActorId("worker-01")
    harness.blackboard.write(
        ActorId("worker-02"),
        {
            "events/worker-02/00001.json": (
                b'{"local_id": "worker-02/00001", "ts": 1.0, "actor": "worker-02", '
                b'"role": "worker", "type": "claim_status", "target": "c-x", '
                b'"to": "corroborated"}'
            )
        },
        "numbered by the blackboard, not yet in the index",
    )
    assert harness.blackboard.head_sequence() == 1
    assert harness.tools.indexer.head_sequence() == 0, "the index lags the blackboard"
    harness.tools.register_agent(worker, tmp_path)
    assert harness.tools.state(worker).cursor == 0
