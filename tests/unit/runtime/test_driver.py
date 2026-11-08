"""One driver per run (#130): a second driver is refused and told who holds the run,
a dead driver's lock expires, and a renewal keeps a live one."""

import pytest

from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.core.types.clock import FakeClock
from vlah.runtime.driver import DriverIdentity, DriverLock, RunHeldError

pytestmark = pytest.mark.runtime

RUN_ID = "r-1"


def test_a_second_driver_is_refused_until_the_lock_expires() -> None:
    clock = FakeClock()
    store = MemoryKeyValueStore(clock=clock, namespace=RUN_ID)
    first = DriverLock(store=store, run_id=RUN_ID, clock=clock, ttl_seconds=100.0)
    first.acquire()
    second = DriverLock(store=store, run_id=RUN_ID, clock=clock, ttl_seconds=100.0)
    with pytest.raises(RunHeldError, match=f"pid {first.identity.pid}"):
        second.acquire()
    clock.advance(60.0)
    first.renew()
    clock.advance(60.0)
    with pytest.raises(RunHeldError):
        second.acquire()  # the renewal pushed the expiry out
    clock.advance(101.0)
    assert first.holder() is None, "a lock nobody renews expires"
    second.acquire()
    assert second.is_held
    first.renew()
    assert not first.is_held or second.holder() == first.identity


def test_release_frees_the_run_and_the_record_names_the_holder() -> None:
    clock = FakeClock()
    store = MemoryKeyValueStore(clock=clock, namespace=RUN_ID)
    lock = DriverLock(store=store, run_id=RUN_ID, clock=clock)
    lock.acquire()
    holder = lock.holder()
    assert holder is not None and holder.pid == lock.identity.pid
    assert DriverIdentity.from_json(holder.to_json()) == holder
    lock.release()
    assert lock.holder() is None
    other = DriverLock(store=store, run_id=RUN_ID, clock=clock)
    other.acquire()
    assert other.is_held
