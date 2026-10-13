"""The chatbot's key-value store: Redis when REDIS_URL is set, otherwise a small
in-process stand-in with the same three calls (tests, a laptop uvicorn).

Used by the answer cache (chatbot/cache.py) and Idempotency-Key handling
(chatbot/idempotency.py). Both treat the store as optional: if Redis is down they
carry on without it (counted in chatbot_store_errors_total), so a Redis outage
costs extra model calls, never a failed request.
"""
import threading
import time

import redis

from etl import config

# Short timeouts: a dead Redis should cost a request milliseconds, not seconds.
TIMEOUT_S = 0.25


class MemoryStore:
    """get / set(ex=, nx=) / delete with expiry, like the subset of redis-py we use."""

    def __init__(self):
        self._data: dict[str, tuple[float | None, str]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] is not None and time.monotonic() >= hit[0]:
                del self._data[key]
                hit = None
            return hit[1] if hit else None

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None:
        with self._lock:
            hit = self._data.get(key)
            if nx and hit and (hit[0] is None or time.monotonic() < hit[0]):
                return None                      # redis-py returns None when NX finds the key
            self._data[key] = (time.monotonic() + ex if ex else None, value)
            return True

    def delete(self, key: str) -> int:
        with self._lock:
            return 1 if self._data.pop(key, None) else 0


_store = None


def get():
    """The process-wide store, created on first use."""
    global _store
    if _store is None:
        _store = (redis.Redis.from_url(config.REDIS_URL, decode_responses=True, socket_timeout=TIMEOUT_S,
                                       socket_connect_timeout=TIMEOUT_S)
                  if config.REDIS_URL else MemoryStore())
    return _store


def reset(store=None) -> None:
    """Swap the store (tests pass a fakeredis or a fresh MemoryStore)."""
    global _store
    _store = store
