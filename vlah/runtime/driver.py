"""One driver per run: the lock that keeps two control planes off one run (D56, #130).

With the run's state in a shared store, a second vlah run --resume could drive the
same run: two merge queues, two assigners of sequence numbers. The driver takes
run:<id>:driver in the store with compare-and-set before anything else, renews it
while it waits on the run, and releases it at the end. A lock whose driver died
expires after its TTL, and the next resume takes it. The value names the holder,
so a refusal says who has the run.
"""

import json
import os
import socket
from dataclasses import dataclass, field

from vlah.core.types.clock import Clock
from vlah.ports.store import KeyValueStore

DEFAULT_TTL_SECONDS = 120.0


class RunHeldError(ValueError):
    """Another driver holds the run; the CLI reports it like any refused request."""


@dataclass(frozen=True, kw_only=True)
class DriverIdentity:
    hostname: str
    pid: int
    started_seconds: float

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "hostname": self.hostname,
                "pid": self.pid,
                "started_seconds": self.started_seconds,
            },
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, text: bytes) -> "DriverIdentity":
        value: object = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError(f"a driver record is a JSON object, not {text!r}")
        else:
            return cls(
                hostname=str(value.get("hostname", "")),
                pid=int(value.get("pid", 0)),
                started_seconds=float(value.get("started_seconds", 0.0)),
            )

    def describe(self) -> str:
        return f"{self.hostname} pid {self.pid}, started at {self.started_seconds:.0f}"


def this_driver(clock: Clock) -> DriverIdentity:
    return DriverIdentity(
        hostname=socket.gethostname(),
        pid=os.getpid(),
        started_seconds=clock.now_seconds(),
    )


@dataclass(kw_only=True)
class DriverLock:
    store: KeyValueStore
    run_id: str
    clock: Clock
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    identity: DriverIdentity = field(init=False)
    is_held: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.identity = this_driver(self.clock)

    @property
    def key(self) -> str:
        return f"run:{self.run_id}:driver"

    def holder(self) -> DriverIdentity | None:
        """Who holds the run now, or None; an expired record counts as nobody."""
        record = self.store.get(self.key)
        return DriverIdentity.from_json(record) if record is not None else None

    def acquire(self) -> None:
        """Take the run; raise RunHeldError naming the holder if another driver has it."""
        if self.store.compare_and_set(
            self.key, None, self.identity.to_json(), self.ttl_seconds
        ):
            self.is_held = True
        else:
            holder = self.holder()
            raise RunHeldError(
                f"run {self.run_id} is driven by {holder.describe()}; "
                f"its lock expires {self.ttl_seconds:.0f} s after its last renewal"
                if holder is not None
                else f"run {self.run_id} is driven by another process"
            )

    def renew(self) -> None:
        """Push the expiry out; a lock lost in the meantime is taken again if free."""
        if not self.is_held:
            return
        elif not self.store.compare_and_set(
            self.key, self.identity.to_json(), self.identity.to_json(), self.ttl_seconds
        ):
            self.is_held = False
            self.acquire()
        else:
            pass

    def release(self) -> None:
        if self.is_held:
            self.store.delete(self.key)
            self.is_held = False
        else:
            pass
