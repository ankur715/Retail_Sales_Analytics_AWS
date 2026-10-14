"""Answer cache: the same question (case and spacing ignored) within 15 minutes
gets the stored answer -- no model call and no query.

Entries live in the store (Redis in the compose stack, so they survive a restart and
are shared by every chatbot replica) under

    chat:answer:<data version>:<caller's brand scope>:<sha256 of the normalized question>

The brand scope (milestone 4, chatbot/access.py) keeps one brand's answers from ever being
served to another: the same question asked by Aurora and by Cobalt are different entries.

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
STALE_TTL = 24 * 3600  # seconds the last answer per question is kept as a fallback (last())
VERSION_TTL = 60       # seconds between data-version lookups
VERSION_RETRY = 10     # seconds before retrying a failed lookup (don't hammer a down database)
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
        with _lock:
            if _version:   # keep the last known version, and look again in VERSION_RETRY seconds
                _version = (time.monotonic() - VERSION_TTL + VERSION_RETRY, _version[1])
            return _version[1] if _version else None   # stale beats nothing while the database is down
    with _lock:
        _version = (time.monotonic(), version)
    return version


def invalidate() -> None:
    """Forget the data version, so the next request looks it up (and misses if it changed)."""
    global _version
    with _lock:
        _version = None


def _hash(question: str) -> str:
    return hashlib.sha256(normalize(question).encode()).hexdigest()[:32]


def _key(question: str, version: str, scope: str = "all") -> str:
    return f"chat:answer:{version}:{scope}:{_hash(question)}"


def last(question: str, scope: str = "all") -> dict | None:
    """The most recent answer to this question from ANY data version (kept 24 h): the
    fallback when the model or the database is down. Its data_as_of says how old it is."""
    try:
        raw = store.get().get(f"chat:last:{scope}:{_hash(question)}")
    except Exception:
        telemetry.STORE_ERRORS.labels("cache_last").inc()
        return None
    return json.loads(raw) if raw else None


def get(question: str, version: str | None, scope: str = "all") -> dict | None:
    if version is None:
        return None
    try:
        with telemetry.step("cache.get"):
            raw = store.get().get(_key(question, version, scope))
    except Exception:
        telemetry.STORE_ERRORS.labels("cache_get").inc()
        return None
    return json.loads(raw) if raw else None


def put(question: str, version: str | None, payload: dict, scope: str = "all") -> None:
    """Store under the version read at the START of the request: if a load lands
    mid-request, the answer was computed from the older data and must not be
    filed under the new version."""
    if version is None:
        return
    try:
        raw = json.dumps(payload)
        store.get().set(_key(question, version, scope), raw, ex=TTL)
        store.get().set(f"chat:last:{scope}:{_hash(question)}", raw, ex=STALE_TTL)
    except Exception:
        telemetry.STORE_ERRORS.labels("cache_put").inc()
