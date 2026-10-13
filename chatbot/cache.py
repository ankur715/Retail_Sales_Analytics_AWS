"""Answer cache: the same question (case and spacing ignored) within 15 minutes
gets the stored answer -- no model call and no query.

Entries live in the store (Redis in the compose stack, so they survive a restart and
are shared by every chatbot replica) under

    chat:answer:<data version>:<sha256 of the normalized question>

The data version is the latest successful load time, MAX(last_successful_load_at)
from chat.v_data_freshness: a new load changes it, so answers computed from older
data stop matching and simply expire. The version is looked up at most once a
minute per process (VERSION_TTL), so an answer can trail a new load by up to a
minute; invalidate() drops it at once (milestone 3 wires that to the load event).

If the version lookup fails, the last known version is used, so cached answers keep
being served while the database is down. If the store fails, the request runs
uncached.
"""
import hashlib
import json
import threading
import time

from chatbot import db, store, telemetry

TTL = 15 * 60          # seconds an answer stays cached
VERSION_TTL = 60       # seconds between data-version lookups
_version: tuple[float, str] | None = None   # (looked up at, version)
_lock = threading.Lock()


def normalize(question: str) -> str:
    return " ".join(question.lower().split())


def data_version() -> str | None:
    global _version
    with _lock:
        if _version and time.monotonic() - _version[0] < VERSION_TTL:
            return _version[1]
    try:
        with telemetry.step("db.data_version"):
            version = str(db.data_version() or "none")
    except Exception:
        telemetry.STORE_ERRORS.labels("data_version").inc()
        return _version[1] if _version else None   # stale beats nothing while the database is down
    with _lock:
        _version = (time.monotonic(), version)
    return version


def invalidate() -> None:
    """Forget the data version, so the next request looks it up (and misses if it changed)."""
    global _version
    with _lock:
        _version = None


def _key(question: str, version: str) -> str:
    return f"chat:answer:{version}:{hashlib.sha256(normalize(question).encode()).hexdigest()[:32]}"


def get(question: str, version: str | None) -> dict | None:
    if version is None:
        return None
    try:
        with telemetry.step("cache.get"):
            raw = store.get().get(_key(question, version))
    except Exception:
        telemetry.STORE_ERRORS.labels("cache_get").inc()
        return None
    return json.loads(raw) if raw else None


def put(question: str, version: str | None, payload: dict) -> None:
    """Store under the version read at the START of the request: if a load lands
    mid-request, the answer was computed from the older data and must not be
    filed under the new version."""
    if version is None:
        return
    try:
        store.get().set(_key(question, version), json.dumps(payload), ex=TTL)
    except Exception:
        telemetry.STORE_ERRORS.labels("cache_put").inc()
