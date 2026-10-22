"""The blackboard's write checks, merge ordering, mirrors and layout (v2.2 section 5, D31, D33, D56, D67)."""

import subprocess
import threading
from pathlib import Path

import pytest

from vlah.adapters.memory.store import MemoryObjectStore, MemoryVersionedStore
from vlah.adapters.store.git.store import GitVersionedStore
from vlah.core.blackboard.api import Blackboard, MergedCommit, PublicProblemFiles
from vlah.core.blackboard.blackboard import StoreBlackboard
from vlah.core.blackboard.layout import init_layout, open_blackboard
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import Event, event_to_json
from vlah.core.types.ids import ActorId, format_claim_id, format_event_local_id
from vlah.ports.store import VersionedStore
from vlah.testing.conformance import check_versioned_store

pytestmark = pytest.mark.blackboard

WORKER = ActorId("worker-07")
PUSH_TOKEN = "test-token"


def claim_file(
    author: ActorId, number: int, evidence: EvidenceRef
) -> tuple[str, bytes]:
    claim = Claim(
        id=format_claim_id(author, number),
        type="insight",
        status="proposed",
        approach="a-01",
        author=author,
        evidence=(evidence,),
        body="An insight.",
    )
    return f"claims/{author}/{claim.id}.md", render_claim(claim).encode("utf-8")


def event_file(
    actor: ActorId, number: int, seq: int | None = None
) -> tuple[str, bytes]:
    event = Event(
        seq=seq,
        local_id=format_event_local_id(actor, number),
        ts=1.0,
        actor=actor,
        role="worker",
        type="claim_status",
        target="c-x",
        to="corroborated",
    )
    return f"events/{event.local_id}.json", event_to_json(event).encode("utf-8")


class MemoryMirrorStores:
    def __call__(self, mirror_name: str) -> tuple[VersionedStore, str]:
        return MemoryVersionedStore(name=mirror_name, push_token=PUSH_TOKEN), PUSH_TOKEN


def memory_blackboard(objects: MemoryObjectStore | None = None) -> StoreBlackboard:
    return StoreBlackboard(
        store=MemoryVersionedStore(name="blackboard", push_token=PUSH_TOKEN),
        push_token=PUSH_TOKEN,
        objects=objects,
        mirror_store_factory=MemoryMirrorStores(),
        clock=FakeClock(),
    )


def test_git_store_passes_the_versioned_store_check(tmp_path: Path) -> None:
    check_versioned_store(
        GitVersionedStore(repository_path=tmp_path / "bb.git", push_token=PUSH_TOKEN),
        PUSH_TOKEN,
    )


def test_git_store_rebuilds_its_sequence_index_on_reopen(tmp_path: Path) -> None:
    """After a restart, sequence numbers must continue, never restart at 1 (D56)."""
    blackboard = open_blackboard(tmp_path, "run-1", "blackboard")
    blackboard.write(WORKER, dict([event_file(WORKER, 1)]), "first")
    reopened = open_blackboard(tmp_path, "run-1", "blackboard")
    receipt = reopened.write(WORKER, dict([event_file(WORKER, 2)]), "second")
    assert receipt.sequence_numbers == (2,)


@pytest.mark.parametrize(
    ("actor", "files", "rule"),
    [
        (
            WORKER,
            dict([claim_file(ActorId("worker-08"), 1, EvidenceRef(path="spec.md"))]),
            "path_not_allowed",
        ),
        (WORKER, {"candidates/9f3a/result.json": b"{}"}, "path_not_allowed"),
        (ActorId("eval_service-x"), {"claims/x/c.md": b"x"}, "path_not_allowed"),
        (WORKER, dict([event_file(WORKER, 1, seq=5)]), "invalid_event"),
        (
            WORKER,
            {
                f"claims/{WORKER}/c-worker-07-00001.md": b"---\nid: c-worker-07-00001\n"
                b"type: insight\nstatus: proposed\napproach: a-01\nauthor: worker-07\n"
                b"evidence: []\n---\n"
            },
            "missing_evidence",
        ),
        (
            WORKER,
            dict(
                [claim_file(WORKER, 1, EvidenceRef(trace="traces/worker-07/t.ptr#L1"))]
            ),
            "dangling_evidence",
        ),
        (
            WORKER,
            {"candidates/9f3a/manifest.yaml": b"kind: patch\n"},
            "invalid_manifest",
        ),
    ],
)
def test_bad_writes_are_rejected_without_committing(
    actor: ActorId, files: dict[str, bytes], rule: str
) -> None:
    blackboard = memory_blackboard()
    receipt = blackboard.write(actor, files, "bad")
    assert receipt.status == "rejected"
    assert {violation.rule for violation in receipt.violations} == {rule}
    assert blackboard.list_paths("", None) == ()


def test_existing_files_are_never_overwritten() -> None:
    blackboard = memory_blackboard()
    path, content = claim_file(WORKER, 1, EvidenceRef(path="spec.md"))
    blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    assert blackboard.write(WORKER, {path: content}, "first").status == "merged"
    second = blackboard.write(WORKER, {path: content}, "again")
    assert [violation.rule for violation in second.violations] == ["file_exists"]


def test_evidence_resolves_on_main_in_the_same_write_or_in_the_archive() -> None:
    objects = MemoryObjectStore()
    trace_pointer = objects.put("traces/worker-07/t-1", b"trace", False)
    blackboard = memory_blackboard(objects)
    first_path, first_content = claim_file(WORKER, 1, EvidenceRef(trace=trace_pointer))
    second_path, second_content = claim_file(
        WORKER, 2, EvidenceRef(claim=format_claim_id(WORKER, 1))
    )
    receipt = blackboard.write(
        WORKER, {first_path: first_content, second_path: second_content}, "two claims"
    )
    assert receipt.status == "merged", receipt.violations


def test_concurrent_writers_lose_nothing_and_sequence_numbers_have_no_gaps(
    tmp_path: Path,
) -> None:
    blackboard = open_blackboard(tmp_path, "run-1", "blackboard")
    writers = [ActorId(f"worker-{index:02d}") for index in range(1, 51)]
    barrier = threading.Barrier(len(writers))

    def write_one(writer: ActorId) -> None:
        barrier.wait()
        blackboard.write(writer, dict([event_file(writer, 1)]), f"event from {writer}")

    threads = [threading.Thread(target=write_one, args=(writer,)) for writer in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert blackboard.head_sequence() == 50
    pointed_paths = {
        blackboard.read(f"events/_seq/{sequence}", None).decode()
        for sequence in range(1, 51)
    }
    assert pointed_paths == {f"events/{writer}/00001.json" for writer in writers}


def test_concurrent_merges_reach_listeners_in_sequence_order(tmp_path: Path) -> None:
    """Delivery order is the contract: replay and routing assume it (D56)."""
    blackboard = open_blackboard(tmp_path, "run-1", "blackboard")
    delivered: list[int] = []
    blackboard.subscribe(
        lambda merged_commit: delivered.extend(
            event.seq for event in merged_commit.events if event.seq is not None
        )
    )
    writers = [ActorId(f"worker-{index:02d}") for index in range(1, 21)]
    barrier = threading.Barrier(len(writers))

    def write_one(writer: ActorId) -> None:
        barrier.wait()
        blackboard.write(writer, dict([event_file(writer, 1)]), f"event from {writer}")

    threads = [threading.Thread(target=write_one, args=(writer,)) for writer in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert delivered == list(range(1, 21))


def test_a_listener_that_writes_neither_deadlocks_nor_overtakes() -> None:
    """The indexer writes corroboration events from inside its merge hook (W1-02)."""
    blackboard = memory_blackboard()
    reacted: list[bool] = []

    def react_once(merged_commit: MergedCommit) -> None:
        if not reacted:
            reacted.append(True)
            blackboard.write(WORKER, dict([event_file(WORKER, 2)]), "reaction")
        else:
            pass

    second_listener_saw: list[int] = []
    blackboard.subscribe(react_once)
    blackboard.subscribe(
        lambda merged_commit: second_listener_saw.extend(
            event.seq for event in merged_commit.events if event.seq is not None
        )
    )
    blackboard.write(WORKER, dict([event_file(WORKER, 1)]), "first")
    assert second_listener_saw == [1, 2]


def test_reading_at_a_sequence_number_shows_that_moment() -> None:
    blackboard = memory_blackboard()
    blackboard.write(WORKER, dict([event_file(WORKER, 1)]), "first")
    blackboard.write(WORKER, dict([event_file(WORKER, 2)]), "second")
    assert blackboard.list_paths(f"events/{WORKER}/", 1) == (
        f"events/{WORKER}/00001.json",
    )


def test_listeners_get_events_with_their_sequence_numbers() -> None:
    blackboard = memory_blackboard()
    received: list[tuple[int | None, ...]] = []
    blackboard.subscribe(
        lambda merged_commit: received.append(
            tuple(event.seq for event in merged_commit.events)
        )
    )
    blackboard.write(
        WORKER, dict([event_file(WORKER, 2), event_file(WORKER, 1)]), "two"
    )
    assert received == [(1, 2)]


def test_a_push_without_the_token_is_rejected(tmp_path: Path) -> None:
    """Workers clone the blackboard but can never push to it (D68)."""
    blackboard = open_blackboard(tmp_path, "run-1", "blackboard")
    blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", blackboard.clone_url(), str(clone)], check=True
    )
    (clone / "claims").mkdir()
    (clone / "claims" / "forged.md").write_text("forged")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.name=w",
            "-c",
            "user.email=w@x",
            "commit",
            "-qm",
            "x",
        ],
        check=True,
    )
    push = subprocess.run(
        ["git", "-C", str(clone), "push", "-q", "origin", "HEAD:main"],
        capture_output=True,
        check=False,
    )
    assert push.returncode != 0
    assert b"push token" in push.stderr


def test_mirror_holds_only_kept_paths_and_follows_merges() -> None:
    blackboard = memory_blackboard()
    blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    mirror: Blackboard = blackboard.mirror(
        "s1", lambda path: path == "spec.md" or path.startswith("events/")
    )
    path, content = claim_file(WORKER, 1, EvidenceRef(path="spec.md"))
    blackboard.write(
        WORKER, {path: content, **dict([event_file(WORKER, 1)])}, "claim and event"
    )
    mirrored_paths = tuple(
        path
        for path in mirror.list_paths("", None)
        if not path.startswith("events/_seq/")
    )
    assert mirrored_paths == ("events/worker-07/00001.json", "spec.md")
    assert mirror.write(WORKER, {"spec.md": b"x"}, "x").status == "rejected"


def test_layout_never_copies_hidden_files() -> None:
    blackboard = memory_blackboard()
    with pytest.raises(ValueError, match="never copy"):
        init_layout(
            blackboard,
            PublicProblemFiles(
                root=Path("problem"),
                files={
                    "spec.md": b"spec",
                    "herd.yaml": b"judges: {}",
                    "private/x": b"y",
                },
            ),
        )
    init_layout(
        blackboard,
        PublicProblemFiles(
            root=Path("problem"),
            files={
                "spec.md": b"spec",
                "charter/status.md": b"aligned",
                "approaches/a-01.md": b"a",
            },
        ),
    )
    assert blackboard.list_paths("", None) == (
        "approaches/a-01.md",
        "charter/status.md",
        "spec.md",
    )
