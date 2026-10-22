"""The blackboard over a VersionedStore: checks, one merge at a time, parsed commits (D30, D31, D33, D56).

Every write is checked synchronously before anything is committed:
- the path is allowed for the writer;
- nothing that exists is changed, and no file takes a directory's name or sits
  inside a file's name (git would delete what is there);
- claims, events and manifests parse;
- every evidence pointer resolves.

Writes are merged one at a time, and subscribers get each merge parsed: events
carry the sequence numbers they were given. Delivery is strictly in sequence
order: merges enter a queue under the merge lock, and one thread at a time drains
it. A listener that writes only enqueues, so it never deadlocks or overtakes.
"""

import logging
import re
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Protocol

import yaml
from pydantic import ValidationError

from vlah.core.blackboard.api import (
    Blackboard,
    CommitListener,
    MergedCommit,
    PathFilter,
    Violation,
    WriteReceipt,
)
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.claims import Claim, EvidenceRef, parse_claim
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event, event_from_json
from vlah.core.types.ids import ActorId, role_of_actor
from vlah.core.types.paths import may_create
from vlah.ports.errors import NotSupportedError
from vlah.ports.store import MergeResult, ObjectStore, VersionedStore

CLAIM_PATH_PATTERN = re.compile(r"claims/(?P<owner>[^/]+)/(?P<name>[^/]+)\.md")
EVENT_PATH_PATTERN = re.compile(
    r"events/(?P<owner>(?!_seq/)[^/]+)/(?P<name>[^/]+)\.json"
)
MANIFEST_PATH_PATTERN = re.compile(r"candidates/(?P<hash>[^/]+)/manifest\.yaml")
CANDIDATE_PATH_PATTERN = re.compile(r"candidates/(?P<hash>[^/]+)/.+")
SEQUENCE_POINTER_PREFIX = "events/_seq/"
OBJECT_POINTER_PREFIXES = ("object://", "private://")

logger = logging.getLogger(__name__)


class MirrorStoreFactory(Protocol):
    def __call__(self, mirror_name: str) -> tuple[VersionedStore, str]:
        """A fresh store for a mirror, and its push token."""
        ...


@dataclass(frozen=True, kw_only=True)
class BlackboardStats:
    """Merge counts and durations, the H4 signal (merge-queue backlog)."""

    merge_count: int
    total_merge_seconds: float
    max_merge_seconds: float


@dataclass(kw_only=True)
class CheckedFiles:
    """What a successful check parsed, so write does not parse twice."""

    claims: dict[str, Claim] = field(default_factory=dict[str, Claim])
    events: dict[str, Event] = field(default_factory=dict[str, Event])


def strip_fragment(pointer: str) -> str:
    return pointer.split("#", 1)[0]


def evidence_pointer(evidence: EvidenceRef) -> str:
    for pointer in (evidence.trace, evidence.candidate, evidence.path, evidence.claim):
        if pointer is not None:
            return pointer
        else:
            continue
    raise ValueError("evidence has no pointer")


def deliver(merged_commit: MergedCommit, listeners: list[CommitListener]) -> None:
    """Give every listener the commit; one that raises is logged and skips only it.

    Delivery runs on whichever writer is draining, so an exception must neither stop
    the listeners after it nor surface in that writer's already merged write. A
    listener that needs every commit handles its own errors, or rebuilds from the
    blackboard (D13).
    """
    for listener in listeners:
        try:
            listener(merged_commit)
        except Exception:
            logger.exception(
                f"a merge listener failed on commit {merged_commit.commit_id}; "
                "the other listeners still get it"
            )


class StoreBlackboard:
    """The blackboard; merges are serialized, so sequence numbers only ever increase."""

    def __init__(
        self,
        *,
        store: VersionedStore,
        push_token: str,
        objects: ObjectStore | None,
        mirror_store_factory: MirrorStoreFactory,
        clock: Clock,
    ) -> None:
        self.store = store
        self.push_token = push_token
        self.objects = objects
        self.mirror_store_factory = mirror_store_factory
        self.clock = clock
        self.lock = threading.Lock()
        self.listeners: dict[int, CommitListener] = {}
        self.next_subscription_id = 1
        self.delivery_lock = threading.Lock()
        self.delivery_queue: deque[MergedCommit] = deque()
        self.is_delivering = False
        self.claim_paths_by_id: dict[str, str] = {}
        for path in store.list_paths("claims/", None):
            claim_match = CLAIM_PATH_PATTERN.fullmatch(path)
            if claim_match is not None:
                self.claim_paths_by_id[claim_match.group("name")] = path
            else:
                pass
        self.merge_count = 0
        self.total_merge_seconds = 0.0
        self.max_merge_seconds = 0.0

    def check(
        self, actor: ActorId, files: Mapping[str, bytes]
    ) -> tuple[Violation, ...]:
        return self.check_and_parse(actor, files)[0]

    def check_and_parse(
        self, actor: ActorId, files: Mapping[str, bytes]
    ) -> tuple[tuple[Violation, ...], CheckedFiles]:
        violations: list[Violation] = []
        checked = CheckedFiles()
        for path, content in sorted(files.items()):
            violation = self.check_one(actor, path, content, files, checked)
            if violation is not None:
                violations.append(violation)
            else:
                pass
        for path, claim in sorted(checked.claims.items()):
            for evidence in claim.evidence:
                if not self.resolves(evidence, files, checked):
                    violations.append(
                        Violation(
                            path=path,
                            rule="dangling_evidence",
                            message=f"evidence {evidence_pointer(evidence)!r} does not resolve",
                        )
                    )
                else:
                    pass
        return tuple(violations), checked

    def check_one(
        self,
        actor: ActorId,
        path: str,
        content: bytes,
        files: Mapping[str, bytes],
        checked: CheckedFiles,
    ) -> Violation | None:
        try:
            is_allowed = may_create(actor, path)
        except ValueError as error:
            return Violation(path=path, rule="path_not_allowed", message=str(error))
        if not is_allowed:
            return Violation(
                path=path,
                rule="path_not_allowed",
                message=f"{actor} may not create {path}",
            )
        elif self.store.exists(path):
            return Violation(
                path=path, rule="file_exists", message="files are append-only"
            )
        elif self.is_directory(path, files):
            return Violation(
                path=path,
                rule="file_exists",
                message=f"{path} is a directory; files are append-only",
            )
        elif (ancestor_file := self.file_among_ancestors(path, files)) is not None:
            return Violation(
                path=path,
                rule="file_exists",
                message=f"{ancestor_file} is a file; files are append-only",
            )
        elif CLAIM_PATH_PATTERN.fullmatch(path):
            return self.check_claim(path, content, checked)
        elif EVENT_PATH_PATTERN.fullmatch(path):
            return self.check_event(path, content, checked)
        elif MANIFEST_PATH_PATTERN.fullmatch(path):
            return self.check_manifest(path, content)
        else:
            return None

    def is_directory(self, path: str, files: Mapping[str, bytes]) -> bool:
        """Whether a file on main or in this write lies under path."""
        prefix = f"{path}/"
        return any(written.startswith(prefix) for written in files) or bool(
            self.store.list_paths(prefix, None)
        )

    def file_among_ancestors(self, path: str, files: Mapping[str, bytes]) -> str | None:
        """The nearest directory of path that is already a file, on main or in this write."""
        for parent in PurePosixPath(path).parents:
            ancestor = str(parent)
            if ancestor != "." and (ancestor in files or self.store.exists(ancestor)):
                return ancestor
            else:
                continue
        return None

    def check_claim(
        self, path: str, content: bytes, checked: CheckedFiles
    ) -> Violation | None:
        claim_match = CLAIM_PATH_PATTERN.fullmatch(path)
        assert claim_match is not None
        try:
            claim = parse_claim(content.decode("utf-8"))
        except ValidationError as error:
            if any(detail["loc"][:1] == ("evidence",) for detail in error.errors()):
                return Violation(path=path, rule="missing_evidence", message=str(error))
            else:
                return Violation(path=path, rule="invalid_claim", message=str(error))
        except (ValueError, UnicodeDecodeError) as error:
            return Violation(path=path, rule="invalid_claim", message=str(error))
        if claim.author != claim_match.group("owner"):
            return Violation(
                path=path,
                rule="invalid_claim",
                message="the author must own the directory",
            )
        elif claim.id != claim_match.group("name"):
            return Violation(
                path=path,
                rule="invalid_claim",
                message="the file name must be the claim id",
            )
        elif claim.id in self.claim_paths_by_id:
            return Violation(
                path=path, rule="file_exists", message=f"claim {claim.id} exists"
            )
        else:
            checked.claims[path] = claim
            return None

    def check_event(
        self, path: str, content: bytes, checked: CheckedFiles
    ) -> Violation | None:
        event_match = EVENT_PATH_PATTERN.fullmatch(path)
        assert event_match is not None
        owner = event_match.group("owner")
        try:
            event = event_from_json(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            return Violation(path=path, rule="invalid_event", message=str(error))
        if event.seq is not None:
            return Violation(
                path=path, rule="invalid_event", message="writers leave seq empty (D56)"
            )
        elif event.actor != owner:
            return Violation(
                path=path,
                rule="invalid_event",
                message="the actor must own the directory",
            )
        elif event.local_id != f"{owner}/{event_match.group('name')}":
            return Violation(
                path=path,
                rule="invalid_event",
                message="the file name must be the local id",
            )
        elif event.role != role_of_actor(event.actor):
            return Violation(
                path=path, rule="invalid_event", message="the role must match the actor"
            )
        else:
            checked.events[path] = event
            return None

    def check_manifest(self, path: str, content: bytes) -> Violation | None:
        try:
            manifest_data: object = yaml.safe_load(content.decode("utf-8"))
            CandidateManifest.model_validate(manifest_data)
        except (ValueError, UnicodeDecodeError, yaml.YAMLError) as error:
            return Violation(path=path, rule="invalid_manifest", message=str(error))
        return None

    def resolves(
        self, evidence: EvidenceRef, files: Mapping[str, bytes], checked: CheckedFiles
    ) -> bool:
        """Whether an evidence pointer names something on main, in this write, or archived."""
        if evidence.claim is not None:
            written_ids = {claim.id for claim in checked.claims.values()}
            return (
                evidence.claim in self.claim_paths_by_id
                or evidence.claim in written_ids
            )
        else:
            path = strip_fragment(evidence_pointer(evidence))
            candidate_match = CANDIDATE_PATH_PATTERN.fullmatch(path)
            if path.startswith(OBJECT_POINTER_PREFIXES):
                return self.objects is not None and self.objects.exists(path)
            elif path in files or self.store.exists(path):
                return True
            elif candidate_match is not None:
                # A result claim may point at result.json before the eval service writes it;
                # the candidate itself must exist.
                candidate_prefix = f"candidates/{candidate_match.group('hash')}/"
                return any(
                    written.startswith(candidate_prefix) for written in files
                ) or bool(self.store.list_paths(candidate_prefix, None))
            else:
                return False

    def write(
        self, actor: ActorId, files: Mapping[str, bytes], message: str
    ) -> WriteReceipt:
        with self.lock:
            violations, checked = self.check_and_parse(actor, files)
            if violations:
                return WriteReceipt(
                    status="rejected",
                    commit_id=None,
                    sequence_numbers=(),
                    violations=violations,
                )
            else:
                started_seconds = self.clock.now_seconds()
                branch = f"w/{actor}"
                commit_id = self.store.commit(
                    branch, files, actor, message, self.push_token
                )
                merge_results = self.store.merge(branch)
                merged_commits = [
                    self.parsed_commit(merge_result, checked)
                    for merge_result in merge_results
                ]
                for claim_path, claim in checked.claims.items():
                    self.claim_paths_by_id[claim.id] = claim_path
                self.record_merge_duration(self.clock.now_seconds() - started_seconds)
                with self.delivery_lock:
                    self.delivery_queue.extend(merged_commits)
        self.drain_deliveries()
        sequence_numbers = tuple(
            sequence_number
            for merge_result in merge_results
            for sequence_number in merge_result.event_sequence_numbers.values()
        )
        return WriteReceipt(
            status="merged",
            commit_id=commit_id,
            sequence_numbers=tuple(sorted(sequence_numbers)),
            violations=(),
        )

    def drain_deliveries(self) -> None:
        """Deliver queued merges in order; returns at once if another call is draining."""
        with self.delivery_lock:
            if self.is_delivering:
                return
            else:
                self.is_delivering = True
        try:
            while True:
                with self.delivery_lock:
                    if not self.delivery_queue:
                        self.is_delivering = False
                        return
                    else:
                        merged_commit = self.delivery_queue.popleft()
                deliver(merged_commit, list(self.listeners.values()))
        except BaseException:
            with self.delivery_lock:
                self.is_delivering = False
            raise

    def parsed_commit(
        self, merge_result: MergeResult, checked: CheckedFiles
    ) -> MergedCommit:
        events = tuple(
            checked.events[path].model_copy(update={"seq": sequence_number})
            for path, sequence_number in sorted(
                merge_result.event_sequence_numbers.items(), key=lambda entry: entry[1]
            )
        )
        claims = tuple(
            checked.claims[path]
            for path in merge_result.added_paths
            if path in checked.claims
        )
        return MergedCommit(
            commit_id=merge_result.commit_id,
            actor=merge_result.author,
            paths=merge_result.added_paths,
            events=events,
            claims=claims,
        )

    def record_merge_duration(self, merge_seconds: float) -> None:
        self.merge_count += 1
        self.total_merge_seconds += merge_seconds
        self.max_merge_seconds = max(self.max_merge_seconds, merge_seconds)

    def stats(self) -> BlackboardStats:
        return BlackboardStats(
            merge_count=self.merge_count,
            total_merge_seconds=self.total_merge_seconds,
            max_merge_seconds=self.max_merge_seconds,
        )

    def read(self, path: str, at_sequence: int | None) -> bytes:
        return self.store.read(path, at_sequence)

    def list_paths(self, prefix: str, at_sequence: int | None) -> tuple[str, ...]:
        return self.store.list_paths(prefix, at_sequence)

    def head_sequence(self) -> int:
        return self.store.head_sequence()

    def subscribe(self, listener: CommitListener) -> int:
        subscription_id = self.next_subscription_id
        self.next_subscription_id += 1
        self.listeners[subscription_id] = listener
        return subscription_id

    def unsubscribe(self, subscription_id: int) -> None:
        self.listeners.pop(subscription_id)

    def mirror(self, name: str, keep: PathFilter) -> Blackboard:
        mirror_store, mirror_push_token = self.mirror_store_factory(name)
        return MirrorBlackboard(
            source=self, store=mirror_store, push_token=mirror_push_token, keep=keep
        )

    def clone_url(self) -> str:
        return self.store.clone_url(True)


READ_ONLY_VIOLATION_MESSAGE = "mirrors are read-only; write to the source blackboard"


class MirrorBlackboard:
    """A read-only copy of the kept paths that follows every merge of its source (D67).

    Sequence pointers are never copied: the mirror numbers its own events, and
    cursors always refer to the source.
    """

    def __init__(
        self,
        *,
        source: Blackboard,
        store: VersionedStore,
        push_token: str,
        keep: PathFilter,
    ) -> None:
        self.source = source
        self.store = store
        self.push_token = push_token
        self.keep = keep
        self.listeners: dict[int, CommitListener] = {}
        self.next_subscription_id = 1
        self.copy_lock = threading.Lock()
        # Subscribe before the initial copy, so a merge that lands meanwhile is not missed;
        # copy_paths skips files the mirror already has.
        source.subscribe(self.follow)
        self.copy_paths(source.list_paths("", None), "initial copy")

    def is_kept(self, path: str) -> bool:
        return not path.startswith(SEQUENCE_POINTER_PREFIX) and self.keep(path)

    def copy_paths(self, paths: tuple[str, ...], message: str) -> None:
        with self.copy_lock:
            kept_files = {
                path: self.source.read(path, None)
                for path in paths
                if self.is_kept(path) and not self.store.exists(path)
            }
            if kept_files:
                self.store.commit(
                    "mirror",
                    kept_files,
                    ActorId("merge_queue"),
                    message,
                    self.push_token,
                )
                self.store.merge("mirror")
            else:
                pass

    def follow(self, merged_commit: MergedCommit) -> None:
        self.copy_paths(merged_commit.paths, f"mirror of {merged_commit.commit_id}")
        kept_paths = tuple(path for path in merged_commit.paths if self.is_kept(path))
        if kept_paths:
            mirrored = MergedCommit(
                commit_id=merged_commit.commit_id,
                actor=merged_commit.actor,
                paths=kept_paths,
                events=tuple(
                    event
                    for event in merged_commit.events
                    if self.is_kept(f"events/{event.local_id}.json")
                ),
                claims=tuple(
                    claim
                    for claim in merged_commit.claims
                    if self.is_kept(f"claims/{claim.author}/{claim.id}.md")
                ),
            )
            deliver(mirrored, list(self.listeners.values()))
        else:
            pass

    def write(
        self, actor: ActorId, files: Mapping[str, bytes], message: str
    ) -> WriteReceipt:
        return WriteReceipt(
            status="rejected",
            commit_id=None,
            sequence_numbers=(),
            violations=self.check(actor, files),
        )

    def check(
        self, actor: ActorId, files: Mapping[str, bytes]
    ) -> tuple[Violation, ...]:
        return tuple(
            Violation(
                path=path, rule="path_not_allowed", message=READ_ONLY_VIOLATION_MESSAGE
            )
            for path in sorted(files)
        )

    def read(self, path: str, at_sequence: int | None) -> bytes:
        if at_sequence is None:
            return self.store.read(path, None)
        else:
            raise NotSupportedError("mirrors have no history by source sequence number")

    def list_paths(self, prefix: str, at_sequence: int | None) -> tuple[str, ...]:
        if at_sequence is None:
            return self.store.list_paths(prefix, None)
        else:
            raise NotSupportedError("mirrors have no history by source sequence number")

    def head_sequence(self) -> int:
        return self.source.head_sequence()

    def subscribe(self, listener: CommitListener) -> int:
        subscription_id = self.next_subscription_id
        self.next_subscription_id += 1
        self.listeners[subscription_id] = listener
        return subscription_id

    def unsubscribe(self, subscription_id: int) -> None:
        self.listeners.pop(subscription_id)

    def mirror(self, name: str, keep: PathFilter) -> Blackboard:
        raise NotSupportedError("mirror the source blackboard instead")

    def clone_url(self) -> str:
        return self.store.clone_url(True)
