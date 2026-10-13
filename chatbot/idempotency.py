"""Idempotency-Key for POST /v1/chat (and /api/chat): a client that retries a request
-- after a timeout, a dropped connection, a double click -- with the same key gets the
first response back instead of a second run of the three model calls.

    first request with key K      claim K ("processing", 2 min), run, store the 200 (24 h)
    retry, same key + same body   the stored response, with `Idempotent-Replayed: true`
    retry while the first runs    409 Conflict -- retry shortly
    same key, different body      422 -- a key belongs to exactly one request
    first run fails (503, crash)  K is released, so the retry runs for real

Records live in the store (chatbot/store.py) under chat:idem:<sha256 of K>. A request
without the header behaves as before. If Redis is down the request runs without
idempotency (fail open, counted): for a read-only chatbot a duplicate run costs a few
model calls, while refusing service would cost the answer.
"""
import hashlib
import json
from dataclasses import dataclass

from fastapi import HTTPException

from chatbot import store, telemetry

HEADER = "Idempotency-Key"
PROCESSING_TTL = 120          # seconds; longer than a slow request, short enough to recover from a crash
RESULT_TTL = 24 * 3600        # seconds a finished response can be replayed
MAX_KEY_LEN = 255


@dataclass
class Replay:
    status: int
    body: dict


def fingerprint(body: dict) -> str:
    """The request a key belongs to. Body only: /api/chat and /v1/chat are the same operation."""
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _record_key(key: str) -> str:
    return f"chat:idem:{hashlib.sha256(key.encode()).hexdigest()}"


def _check(key: str) -> None:
    if not (0 < len(key) <= MAX_KEY_LEN) or not key.isascii() or not key.isprintable():
        raise HTTPException(400, f"{HEADER} must be 1-{MAX_KEY_LEN} printable ASCII characters")


def begin(key: str, fp: str) -> Replay | bool:
    """Claim the key. Returns a Replay to send back, True if this request owns the key
    and must finish() or release() it, or False if the store is down (run without it)."""
    _check(key)
    record = _record_key(key)
    try:
        for _ in range(2):   # the existing record can expire between our two calls; try once more
            if store.get().set(record, json.dumps({"state": "processing", "fp": fp}), ex=PROCESSING_TTL, nx=True):
                telemetry.IDEMPOTENCY.labels("new").inc()
                return True
            raw = store.get().get(record)
            if raw:
                break
        else:
            raise RuntimeError("idempotency record kept vanishing")
    except HTTPException:
        raise
    except Exception:
        telemetry.STORE_ERRORS.labels("idempotency").inc()
        return False
    existing = json.loads(raw)
    if existing["fp"] != fp:
        telemetry.IDEMPOTENCY.labels("mismatch").inc()
        raise HTTPException(422, f"This {HEADER} was already used for a different request")
    if existing["state"] == "processing":
        telemetry.IDEMPOTENCY.labels("in_progress").inc()
        raise HTTPException(409, "A request with this Idempotency-Key is still being processed -- retry shortly")
    telemetry.IDEMPOTENCY.labels("replay").inc()
    return Replay(existing["status"], existing["body"])


def finish(key: str, fp: str, status: int, body: dict) -> None:
    try:
        store.get().set(_record_key(key), json.dumps({"state": "done", "fp": fp, "status": status, "body": body}),
                        ex=RESULT_TTL)
    except Exception:
        telemetry.STORE_ERRORS.labels("idempotency").inc()


def release(key: str) -> None:
    """Give the key back after a failed run, so the client's retry runs for real."""
    try:
        store.get().delete(_record_key(key))
    except Exception:
        telemetry.STORE_ERRORS.labels("idempotency").inc()   # the claim expires after PROCESSING_TTL anyway
