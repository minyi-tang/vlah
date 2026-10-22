"""The herd's tools: write gates, read scope by arm, recorder events, sync-point diffs (v2.2 section 7)."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.fixtures.tools import make_tools_harness
from vlah.arms.config import PRESETS, ArmName
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.tasks import Task
from vlah.tools.herd_tools import ToolReply

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
