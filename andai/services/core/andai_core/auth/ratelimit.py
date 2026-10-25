"""Sliding-window attempt limiting.

`Limiter` counts hits per key over a window; once a key has `limit` hits inside the window it is
blocked until the oldest of them leaves the window. Login uses it for failed attempts per
(email, client IP); keys are hashed, so the store never holds an email address.

Two implementations with the same behaviour: `MemoryLimiter` (one process; tests) and
`ValkeyLimiter` (the stack's Valkey, shared by every andai-core process).
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Protocol

import redis

LOGIN_LIMIT = 5
LOGIN_WINDOW_SECONDS = 15 * 60


class Limiter(Protocol):
    def blocked(self, key: str) -> int | None:
        """Seconds until `key` may try again, or None if it is not blocked."""

    def hit(self, key: str) -> None:
        """Count one attempt against `key`."""

    def clear(self, key: str) -> None:
        """Forget every attempt counted against `key`."""


def login_key(email: str, ip: str | None) -> str:
    material = f"login\x00{email}\x00{ip or '-'}".encode()
    return "login:" + hashlib.sha256(material).hexdigest()


def reset_key(email: str) -> str:
    return "reset:" + hashlib.sha256(f"reset\x00{email}".encode()).hexdigest()


def _retry_after(oldest: float, window: float, now: float) -> int:
    return max(1, math.ceil(oldest + window - now))


class MemoryLimiter:
    def __init__(
        self,
        limit: int = LOGIN_LIMIT,
        window: float = LOGIN_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _live(self, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return hits

    def blocked(self, key: str) -> int | None:
        now = self.clock()
        with self._lock:
            hits = self._live(key, now)
            if len(hits) < self.limit:
                return None
            return _retry_after(hits[0], self.window, now)

    def hit(self, key: str) -> None:
        now = self.clock()
        with self._lock:
            self._live(key, now).append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class ValkeyLimiter:
    """One sorted set per key (member = a unique hit id, score = hit time); the set expires one
    window after its last hit."""

    def __init__(
        self,
        client: redis.Redis,
        limit: int = LOGIN_LIMIT,
        window: float = LOGIN_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
        prefix: str = "andai:limit:",
    ) -> None:
        self.client = client
        self.limit = limit
        self.window = window
        self.clock = clock
        self.prefix = prefix

    def _name(self, key: str) -> str:
        return self.prefix + key

    def blocked(self, key: str) -> int | None:
        now = self.clock()
        name = self._name(key)
        pipe = self.client.pipeline(transaction=True)
        pipe.zremrangebyscore(name, "-inf", now - self.window)
        pipe.zcard(name)
        pipe.zrange(name, 0, 0, withscores=True)
        _, count, oldest = pipe.execute()
        if count < self.limit:
            return None
        return _retry_after(float(oldest[0][1]), self.window, now)

    def hit(self, key: str) -> None:
        now = self.clock()
        name = self._name(key)
        pipe = self.client.pipeline(transaction=True)
        pipe.zadd(name, {uuid.uuid4().hex: now})
        pipe.zremrangebyscore(name, "-inf", now - self.window)
        pipe.expire(name, math.ceil(self.window))
        pipe.execute()

    def clear(self, key: str) -> None:
        self.client.delete(self._name(key))
