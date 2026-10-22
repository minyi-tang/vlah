"""A KeyValueStore on Redis (v2.2 D32, D67): leases, the task queue and counters.

Each run has its own key namespace. Expiry is decided by the injected clock: every
value is a Redis hash holding the bytes and their expiry time, checked on read.
Redis's own key expiry is also set, but only so abandoned keys are cleaned up.
"""

import math

import redis
from redis.client import Pipeline

from vlah.core.types.clock import Clock

VALUE_FIELD = "value"
EXPIRES_AT_FIELD = "expires_at"
CLEANUP_MARGIN_SECONDS = 60.0


def response_bytes(response: object) -> bytes | None:
    """Narrow a Redis reply, which the client types as bytes or str, to bytes."""
    if response is None:
        return None
    elif isinstance(response, bytes):
        return response
    elif isinstance(response, str):
        return response.encode("utf-8")
    else:
        raise TypeError(f"unexpected Redis reply type {type(response).__name__}")


class RedisKeyValueStore:
    def __init__(self, *, client: redis.Redis, clock: Clock, namespace: str) -> None:
        self.client = client
        self.clock = clock
        self.namespace = namespace

    def value_key(self, key: str) -> str:
        return f"{self.namespace}:k:{key}"

    def sorted_key(self, key: str) -> str:
        return f"{self.namespace}:z:{key}"

    def is_expired(self, expires_at_text: bytes | None) -> bool:
        if expires_at_text is None or expires_at_text == b"":
            return False
        else:
            return float(expires_at_text) <= self.clock.now_seconds()

    def get(self, key: str) -> bytes | None:
        value_reply, expires_at_reply = self.client.hmget(
            self.value_key(key), [VALUE_FIELD, EXPIRES_AT_FIELD]
        )
        value = response_bytes(value_reply)
        if value is None or self.is_expired(response_bytes(expires_at_reply)):
            return None
        else:
            return value

    def write_value(
        self,
        pipeline: Pipeline,
        key: str,
        value: bytes,
        ttl_seconds: float | None,
    ) -> None:
        full_key = self.value_key(key)
        if ttl_seconds is None:
            pipeline.hset(full_key, mapping={VALUE_FIELD: value, EXPIRES_AT_FIELD: b""})
            pipeline.persist(full_key)
        else:
            expires_at_seconds = self.clock.now_seconds() + ttl_seconds
            pipeline.hset(
                full_key,
                mapping={
                    VALUE_FIELD: value,
                    EXPIRES_AT_FIELD: repr(expires_at_seconds),
                },
            )
            pipeline.expire(full_key, math.ceil(ttl_seconds + CLEANUP_MARGIN_SECONDS))

    def set(self, key: str, value: bytes, ttl_seconds: float | None) -> None:
        with self.client.pipeline(transaction=True) as pipeline:
            self.write_value(pipeline, key, value, ttl_seconds)
            pipeline.execute()

    def compare_and_set(
        self, key: str, expected: bytes | None, value: bytes, ttl_seconds: float | None
    ) -> bool:
        full_key = self.value_key(key)
        with self.client.pipeline(transaction=True) as pipeline:
            while True:
                try:
                    pipeline.watch(full_key)
                    if self.get(key) != expected:
                        pipeline.unwatch()
                        return False
                    else:
                        pipeline.multi()
                        self.write_value(pipeline, key, value, ttl_seconds)
                        pipeline.execute()
                        return True
                except redis.WatchError:
                    continue

    def delete(self, key: str) -> bool:
        if self.get(key) is None:
            return False
        else:
            return bool(self.client.delete(self.value_key(key)))

    def scan(self, prefix: str) -> tuple[str, ...]:
        pattern_prefix = self.value_key("")
        keys = [
            (response_bytes(full_key) or b"").decode()[len(pattern_prefix) :]
            for full_key in self.client.scan_iter(match=f"{self.value_key(prefix)}*")
        ]
        return tuple(sorted(key for key in keys if self.get(key) is not None))

    def increment(self, key: str, amount: int) -> int:
        full_key = self.value_key(key)
        with self.client.pipeline(transaction=True) as pipeline:
            while True:
                try:
                    pipeline.watch(full_key)
                    current_value = self.get(key)
                    if current_value is None:
                        new_value = amount
                    else:
                        new_value = int(current_value) + amount
                    pipeline.multi()
                    self.write_value(
                        pipeline, key, str(new_value).encode("ascii"), None
                    )
                    pipeline.execute()
                    return new_value
                except redis.WatchError:
                    continue

    def sorted_add(self, key: str, member: str, score: float) -> None:
        self.client.zadd(self.sorted_key(key), {member: score})

    def sorted_range(self, key: str) -> tuple[tuple[str, float], ...]:
        members = self.client.zrange(self.sorted_key(key), 0, -1, withscores=True)
        return tuple(
            ((response_bytes(member) or b"").decode(), float(score))
            for member, score in members
        )

    def sorted_remove(self, key: str, member: str) -> bool:
        return bool(self.client.zrem(self.sorted_key(key), member))
