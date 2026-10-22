"""In-memory stores: a whole herd's state in one process, for tests and the demo (D61)."""

import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId, Role, role_of_actor
from vlah.ports.errors import (
    AppendOnlyViolationError,
    PushRejectedError,
    ReadPermissionError,
)
from vlah.ports.store import MergeListener, MergeResult, VectorHit

EVENT_FILE_PATTERN = re.compile(r"events/(?!_seq/)[^/]+/[^/]+\.json")
SEQUENCE_POINTER_PREFIX = "events/_seq/"
PRIVATE_POINTER_PREFIX = "private://"
PUBLIC_POINTER_PREFIX = "object://"
PRIVATE_READER_ROLES: frozenset[Role] = frozenset({"eval_service", "critic", "human"})


@dataclass(frozen=True, kw_only=True)
class PendingCommit:
    commit_id: str
    author: ActorId
    message: str
    files: dict[str, bytes]


class MemoryVersionedStore:
    """Branches, a serial merge into main, and a sequence number per new event file."""

    def __init__(self, *, name: str, push_token: str) -> None:
        self.name = name
        self.push_token = push_token
        self.pending_by_branch: dict[str, list[PendingCommit]] = {}
        self.main_files: dict[str, bytes] = {}
        self.snapshots_by_sequence: dict[int, dict[str, bytes]] = {}
        self.last_sequence = 0
        self.commit_count = 0
        self.listeners: dict[int, MergeListener] = {}
        self.next_subscription_id = 1

    def commit(
        self,
        branch: str,
        files: Mapping[str, bytes],
        author: ActorId,
        message: str,
        push_token: str,
    ) -> str:
        if push_token != self.push_token:
            raise PushRejectedError(
                f"commit to {self.name} by {author} has no push token"
            )
        elif not files:
            raise ValueError("a commit needs at least one file")
        else:
            self.commit_count += 1
            commit_id = f"{self.name}-{self.commit_count:06d}"
            pending_commit = PendingCommit(
                commit_id=commit_id, author=author, message=message, files=dict(files)
            )
            self.pending_by_branch.setdefault(branch, []).append(pending_commit)
            return commit_id

    def merge(self, branch: str) -> tuple[MergeResult, ...]:
        merged: list[MergeResult] = []
        pending_commits = self.pending_by_branch.pop(branch, [])
        for index, pending_commit in enumerate(pending_commits):
            existing_paths = sorted(set(pending_commit.files) & set(self.main_files))
            if existing_paths:
                # The offending commit is dropped; later commits stay pending.
                remaining_commits = pending_commits[index + 1 :]
                if remaining_commits:
                    self.pending_by_branch[branch] = remaining_commits
                else:
                    pass
                raise AppendOnlyViolationError(
                    f"{pending_commit.commit_id} would overwrite {existing_paths}"
                )
            else:
                merge_result = self.apply_commit(pending_commit)
                merged.append(merge_result)
                for listener in list(self.listeners.values()):
                    listener(merge_result)
        return tuple(merged)

    def apply_commit(self, pending_commit: PendingCommit) -> MergeResult:
        self.main_files.update(pending_commit.files)
        event_sequence_numbers: dict[str, int] = {}
        for path in sorted(pending_commit.files):
            if EVENT_FILE_PATTERN.fullmatch(path):
                self.last_sequence += 1
                event_sequence_numbers[path] = self.last_sequence
                self.main_files[f"{SEQUENCE_POINTER_PREFIX}{self.last_sequence}"] = (
                    path.encode("utf-8")
                )
            else:
                pass
        snapshot = dict(self.main_files)
        for sequence_number in event_sequence_numbers.values():
            self.snapshots_by_sequence[sequence_number] = snapshot
        pointer_paths = [
            f"{SEQUENCE_POINTER_PREFIX}{sequence_number}"
            for sequence_number in event_sequence_numbers.values()
        ]
        return MergeResult(
            commit_id=pending_commit.commit_id,
            author=pending_commit.author,
            added_paths=tuple(sorted(pending_commit.files)) + tuple(pointer_paths),
            event_sequence_numbers=event_sequence_numbers,
        )

    def files_at(self, at_sequence: int | None) -> dict[str, bytes]:
        if at_sequence is None:
            return self.main_files
        elif at_sequence in self.snapshots_by_sequence:
            return self.snapshots_by_sequence[at_sequence]
        else:
            raise ValueError(f"no merge assigned sequence number {at_sequence}")

    def read(self, path: str, at_sequence: int | None) -> bytes:
        files = self.files_at(at_sequence)
        if path in files:
            return files[path]
        else:
            raise FileNotFoundError(path)

    def list_paths(self, prefix: str, at_sequence: int | None) -> tuple[str, ...]:
        return tuple(
            sorted(
                path for path in self.files_at(at_sequence) if path.startswith(prefix)
            )
        )

    def exists(self, path: str) -> bool:
        return path in self.main_files

    def head_sequence(self) -> int:
        return self.last_sequence

    def subscribe(self, listener: MergeListener) -> int:
        subscription_id = self.next_subscription_id
        self.next_subscription_id += 1
        self.listeners[subscription_id] = listener
        return subscription_id

    def unsubscribe(self, subscription_id: int) -> None:
        self.listeners.pop(subscription_id)

    def clone_url(self, read_only: bool) -> str:
        if read_only:
            return f"memory://{self.name}?read_only=1"
        else:
            return f"memory://{self.name}"


@dataclass(frozen=True, kw_only=True)
class StoredValue:
    value: bytes
    expires_at_seconds: float | None


class MemoryKeyValueStore:
    """Key-value state with TTLs on the injected clock, plus sorted sets.

    Every operation holds one re-entrant lock, so compare_and_set and sorted_remove are
    atomic across threads, as they are in Redis.
    """

    def __init__(self, *, clock: Clock, namespace: str) -> None:
        self.clock = clock
        self.namespace = namespace
        self.lock = threading.RLock()
        self.entries: dict[str, StoredValue] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}

    def live_entry(self, key: str) -> StoredValue | None:
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            elif (
                entry.expires_at_seconds is not None
                and entry.expires_at_seconds <= self.clock.now_seconds()
            ):
                self.entries.pop(key)
                return None
            else:
                return entry

    def get(self, key: str) -> bytes | None:
        with self.lock:
            entry = self.live_entry(key)
            if entry is None:
                return None
            else:
                return entry.value

    def set(self, key: str, value: bytes, ttl_seconds: float | None) -> None:
        with self.lock:
            if ttl_seconds is None:
                expires_at_seconds = None
            else:
                expires_at_seconds = self.clock.now_seconds() + ttl_seconds
            self.entries[key] = StoredValue(
                value=value, expires_at_seconds=expires_at_seconds
            )

    def compare_and_set(
        self, key: str, expected: bytes | None, value: bytes, ttl_seconds: float | None
    ) -> bool:
        with self.lock:
            if self.get(key) == expected:
                self.set(key, value, ttl_seconds)
                return True
            else:
                return False

    def delete(self, key: str) -> bool:
        with self.lock:
            if self.live_entry(key) is None:
                return False
            else:
                self.entries.pop(key)
                return True

    def scan(self, prefix: str) -> tuple[str, ...]:
        with self.lock:
            return tuple(
                sorted(
                    key
                    for key in list(self.entries)
                    if key.startswith(prefix) and self.live_entry(key) is not None
                )
            )

    def increment(self, key: str, amount: int) -> int:
        with self.lock:
            current_value = self.get(key)
            if current_value is None:
                new_value = amount
            else:
                new_value = int(current_value.decode("ascii")) + amount
            self.set(key, str(new_value).encode("ascii"), None)
            return new_value

    def sorted_add(self, key: str, member: str, score: float) -> None:
        with self.lock:
            self.sorted_sets.setdefault(key, {})[member] = score

    def sorted_range(self, key: str) -> tuple[tuple[str, float], ...]:
        with self.lock:
            members = self.sorted_sets.get(key, {})
            return tuple(
                sorted(members.items(), key=lambda entry: (entry[1], entry[0]))
            )

    def sorted_remove(self, key: str, member: str) -> bool:
        with self.lock:
            members = self.sorted_sets.get(key, {})
            if member in members:
                members.pop(member)
                return True
            else:
                return False


class MemoryObjectStore:
    """Archive-only objects; private ones are readable only by the eval service, Critic and humans."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, is_private: bool) -> str:
        if is_private:
            pointer = f"{PRIVATE_POINTER_PREFIX}{key}"
        else:
            pointer = f"{PUBLIC_POINTER_PREFIX}{key}"
        if pointer in self.objects and self.objects[pointer] != data:
            raise AppendOnlyViolationError(f"{pointer} already holds different data")
        else:
            self.objects[pointer] = data
            return pointer

    def get(self, pointer: str, reader: ActorId) -> bytes:
        if pointer.startswith(PRIVATE_POINTER_PREFIX) and (
            role_of_actor(reader) not in PRIVATE_READER_ROLES
        ):
            raise ReadPermissionError(f"{reader} may not read {pointer}")
        elif pointer in self.objects:
            return self.objects[pointer]
        else:
            raise FileNotFoundError(pointer)

    def exists(self, pointer: str) -> bool:
        return pointer in self.objects


def cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    norm_product = math.sqrt(sum(x * x for x in left)) * math.sqrt(
        sum(y * y for y in right)
    )
    if norm_product == 0:
        return 0.0
    else:
        return sum(x * y for x, y in zip(left, right, strict=True)) / norm_product


@dataclass(frozen=True, kw_only=True)
class IndexedVector:
    vector: tuple[float, ...]
    metadata: dict[str, str]


class MemoryVectorIndex:
    """Brute-force cosine search; ties are ordered by item id."""

    def __init__(self) -> None:
        self.items: dict[str, IndexedVector] = {}

    def upsert(
        self, item_id: str, vector: tuple[float, ...], metadata: dict[str, str]
    ) -> None:
        self.items[item_id] = IndexedVector(vector=vector, metadata=dict(metadata))

    def query(
        self, vector: tuple[float, ...], limit: int, where: dict[str, str] | None
    ) -> tuple[VectorHit, ...]:
        hits = [
            VectorHit(
                item_id=item_id,
                similarity=cosine_similarity(vector, indexed.vector),
                metadata=indexed.metadata,
            )
            for item_id, indexed in self.items.items()
            if where is None
            or all(indexed.metadata.get(name) == value for name, value in where.items())
        ]
        hits.sort(key=lambda hit: (-hit.similarity, hit.item_id))
        return tuple(hits[:limit])

    def delete(self, item_id: str) -> bool:
        if item_id in self.items:
            self.items.pop(item_id)
            return True
        else:
            return False
