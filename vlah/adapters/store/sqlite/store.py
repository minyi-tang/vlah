"""A KeyValueStore in one SQLite file (v2.2 D32): leases, the task queue and counters
that survive a crash, with no server to run.

A local run keeps its state in <run root>/state.sqlite. Expiry is decided by the
injected clock, as in the Redis adapter: each row holds its expiry time, checked on
read. Every operation is one immediate transaction under a process lock, so
compare-and-set, increments and sorted-set removals are atomic across threads, and
a second process on the same file waits its turn. The write-ahead log with full
syncs keeps every committed write through a crash or a power cut.
"""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from vlah.core.types.clock import Clock

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS entries ("
    "namespace TEXT NOT NULL, key TEXT NOT NULL, value BLOB NOT NULL, "
    "expires_at_seconds REAL, PRIMARY KEY (namespace, key))",
    "CREATE TABLE IF NOT EXISTS sorted_members ("
    "namespace TEXT NOT NULL, key TEXT NOT NULL, member TEXT NOT NULL, "
    "score REAL NOT NULL, PRIMARY KEY (namespace, key, member))",
)
# How long a second process waits for the file's write lock before an error.
BUSY_TIMEOUT_SECONDS = 30.0


class SqliteKeyValueStore:
    def __init__(self, *, path: Path, clock: Clock, namespace: str) -> None:
        self.path = path
        self.clock = clock
        self.namespace = namespace
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            path,
            timeout=BUSY_TIMEOUT_SECONDS,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        for statement in SCHEMA:
            self.connection.execute(statement)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def live_value(self, connection: sqlite3.Connection, key: str) -> bytes | None:
        row: tuple[bytes, float | None] | None = connection.execute(
            "SELECT value, expires_at_seconds FROM entries "
            "WHERE namespace = ? AND key = ?",
            (self.namespace, key),
        ).fetchone()
        if row is None:
            return None
        elif row[1] is not None and row[1] <= self.clock.now_seconds():
            return None
        else:
            return bytes(row[0])

    def write_value(
        self,
        connection: sqlite3.Connection,
        key: str,
        value: bytes,
        ttl_seconds: float | None,
    ) -> None:
        if ttl_seconds is None:
            expires_at_seconds = None
        else:
            expires_at_seconds = self.clock.now_seconds() + ttl_seconds
        connection.execute(
            "INSERT INTO entries (namespace, key, value, expires_at_seconds) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (namespace, key) DO UPDATE SET "
            "value = excluded.value, expires_at_seconds = excluded.expires_at_seconds",
            (self.namespace, key, value, expires_at_seconds),
        )

    def get(self, key: str) -> bytes | None:
        with self.transaction() as connection:
            return self.live_value(connection, key)

    def set(self, key: str, value: bytes, ttl_seconds: float | None) -> None:
        with self.transaction() as connection:
            self.write_value(connection, key, value, ttl_seconds)

    def compare_and_set(
        self, key: str, expected: bytes | None, value: bytes, ttl_seconds: float | None
    ) -> bool:
        with self.transaction() as connection:
            if self.live_value(connection, key) == expected:
                self.write_value(connection, key, value, ttl_seconds)
                return True
            else:
                return False

    def delete(self, key: str) -> bool:
        with self.transaction() as connection:
            if self.live_value(connection, key) is None:
                return False
            else:
                connection.execute(
                    "DELETE FROM entries WHERE namespace = ? AND key = ?",
                    (self.namespace, key),
                )
                return True

    def scan(self, prefix: str) -> tuple[str, ...]:
        """Live keys that start with prefix, taken literally, in sorted order."""
        with self.transaction() as connection:
            rows: list[tuple[str]] = connection.execute(
                "SELECT key FROM entries WHERE namespace = ? "
                "AND substr(key, 1, ?) = ? "
                "AND (expires_at_seconds IS NULL OR expires_at_seconds > ?) "
                "ORDER BY key",
                (self.namespace, len(prefix), prefix, self.clock.now_seconds()),
            ).fetchall()
            return tuple(row[0] for row in rows)

    def increment(self, key: str, amount: int) -> int:
        with self.transaction() as connection:
            current_value = self.live_value(connection, key)
            if current_value is None:
                new_value = amount
            else:
                new_value = int(current_value.decode("ascii")) + amount
            self.write_value(connection, key, str(new_value).encode("ascii"), None)
            return new_value

    def sorted_add(self, key: str, member: str, score: float) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO sorted_members (namespace, key, member, score) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (namespace, key, member) "
                "DO UPDATE SET score = excluded.score",
                (self.namespace, key, member, score),
            )

    def sorted_range(self, key: str) -> tuple[tuple[str, float], ...]:
        with self.transaction() as connection:
            rows: list[tuple[str, float]] = connection.execute(
                "SELECT member, score FROM sorted_members "
                "WHERE namespace = ? AND key = ? ORDER BY score, member",
                (self.namespace, key),
            ).fetchall()
            return tuple((member, float(score)) for member, score in rows)

    def sorted_remove(self, key: str, member: str) -> bool:
        with self.transaction() as connection:
            removed = connection.execute(
                "DELETE FROM sorted_members "
                "WHERE namespace = ? AND key = ? AND member = ?",
                (self.namespace, key, member),
            )
            return removed.rowcount == 1

    def close(self) -> None:
        with self.lock:
            self.connection.close()
