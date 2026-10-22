"""Store port: versioned blackboard, key-value state, objects and vectors (v2.2 D30, D32, D33, D56).

The versioned store leaks one semantic on purpose (section 4.1): merge sequence
numbers must only ever increase across the whole herd.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from vlah.core.types.ids import ActorId


@dataclass(frozen=True, kw_only=True)
class MergeResult:
    """One merged commit; each new event file got the next sequence number, in path order."""

    commit_id: str
    author: ActorId
    added_paths: tuple[str, ...]
    event_sequence_numbers: dict[str, int]


class MergeListener(Protocol):
    def __call__(self, merge_result: MergeResult) -> None: ...


class VersionedStore(Protocol):
    def commit(
        self,
        branch: str,
        files: Mapping[str, bytes],
        author: ActorId,
        message: str,
        push_token: str,
    ) -> str:
        """Commit new files to a branch; raises PushRejectedError without the push token."""
        ...

    def merge(self, branch: str) -> tuple[MergeResult, ...]:
        """Merge the branch's pending commits into main, one at a time.

        Raises AppendOnlyViolationError if a commit would overwrite an existing file.
        Every new events/<actor>/<name>.json gets the next sequence number, and a
        pointer file events/_seq/<number> that holds the event's path.
        """
        ...

    def read(self, path: str, at_sequence: int | None) -> bytes:
        """File content on main, or as of the merge that assigned at_sequence."""
        ...

    def list_paths(self, prefix: str, at_sequence: int | None) -> tuple[str, ...]: ...

    def exists(self, path: str) -> bool: ...

    def head_sequence(self) -> int:
        """The last sequence number assigned, or 0 before the first event."""
        ...

    def subscribe(self, listener: MergeListener) -> int:
        """Call listener after every merge, in order; returns a subscription id."""
        ...

    def unsubscribe(self, subscription_id: int) -> None: ...

    def clone_url(self, read_only: bool) -> str: ...


class KeyValueStore(Protocol):
    """Fast-changing state such as leases and queues (D32). Values are bytes."""

    def get(self, key: str) -> bytes | None: ...

    def set(self, key: str, value: bytes, ttl_seconds: float | None) -> None: ...

    def compare_and_set(
        self, key: str, expected: bytes | None, value: bytes, ttl_seconds: float | None
    ) -> bool:
        """Set only if the current value equals expected (None means absent)."""
        ...

    def delete(self, key: str) -> bool: ...

    def scan(self, prefix: str) -> tuple[str, ...]: ...

    def increment(self, key: str, amount: int) -> int: ...

    def sorted_add(self, key: str, member: str, score: float) -> None: ...

    def sorted_range(self, key: str) -> tuple[tuple[str, float], ...]:
        """All members, lowest score first; ties ordered by member."""
        ...

    def sorted_remove(self, key: str, member: str) -> bool:
        """Remove member; True only for the caller that actually removed it."""
        ...


class ObjectStore(Protocol):
    """Traces, checkpoints and private judge data (D33, D63)."""

    def put(self, key: str, data: bytes, is_private: bool) -> str:
        """Store data and return its pointer: object://key, or private://key."""
        ...

    def get(self, pointer: str, reader: ActorId) -> bytes:
        """Raises ReadPermissionError if the reader's role may not read private data."""
        ...

    def exists(self, pointer: str) -> bool: ...


@dataclass(frozen=True, kw_only=True)
class VectorHit:
    item_id: str
    similarity: float
    metadata: dict[str, str]


class VectorIndex(Protocol):
    def upsert(
        self, item_id: str, vector: tuple[float, ...], metadata: dict[str, str]
    ) -> None: ...

    def query(
        self, vector: tuple[float, ...], limit: int, where: dict[str, str] | None
    ) -> tuple[VectorHit, ...]:
        """Most similar items first (cosine similarity), filtered by exact metadata."""
        ...

    def delete(self, item_id: str) -> bool: ...
