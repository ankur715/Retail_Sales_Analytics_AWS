"""Milestone 2: /v1 routes, the Redis answer cache, Idempotency-Key and the sqlgen gRPC
service. Redis is fakeredis (the real redis-py code path, no server); gRPC is a real
server on a free local port with the fake LLM. No Redshift, no model."""
import threading

import fakeredis
import grpc
import pytest
import redis
from fastapi.testclient import TestClient

from chatbot import app as chat_app
from chatbot import cache, idempotency, llm, store, telemetry
from sqlgen import client as sqlgen_client
from sqlgen import server as sqlgen_server

SQL = "SELECT retailer, SUM(sales_usd) AS sales_usd FROM chat.v_sales_by_week GROUP BY retailer"


class DownRedis:
    """A Redis that's unreachable: every call raises like redis-py does."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise redis.ConnectionError("Connection refused")
        return fail


@pytest.fixture
def redis_store():
    fake = fakeredis.FakeRedis(decode_responses=True)
    store.reset(fake)
    cache.invalidate()
    yield fake
    store.reset()
    cache.invalidate()


@pytest.fixture
def pipeline(monkeypatch, redis_store):
    """The chat pipeline with every model call and query faked; counts what actually ran."""
    calls = {"write_sql": 0, "query": 0, "version": "2026-10-18 06:00:00"}
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(chat_app.config, "GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(chat_app.config, "SQLGEN_ADDR", "")
    monkeypatch.setattr(llm, "route", lambda q: llm.Route(views=["chat.v_sales_by_week"], reason="totals"))
    monkeypatch.setattr(llm, "summarize", lambda q, sql, cols, rows, as_of: f"{len(rows)} rows")

    def write_sql(*args, **kwargs):
        calls["write_sql"] += 1
        return llm.SqlPlan(sql=SQL, explanation="totals")

    def query(sql):
        calls["query"] += 1
        return {"columns": ["retailer", "sales_usd"], "rows": [["Harbor Mart", 100.0]], "row_count": 1}

    def data_version():
        if isinstance(calls["version"], Exception):
            raise calls["version"]
        return calls["version"]

    monkeypatch.setattr(llm, "write_sql", write_sql)
    monkeypatch.setattr(chat_app.db, "query", query)
    monkeypatch.setattr(chat_app.db, "known_values", lambda cols: {})
    monkeypatch.setattr(chat_app.db, "data_as_of", lambda: "2026-10-17")
    monkeypatch.setattr(chat_app.db, "data_version", data_version)
    return calls


def ask(client, message="Total sales by retailer", key=None, path="/v1/chat"):
    return client.post(path, json={"message": message}, headers={"Idempotency-Key": key} if key else {})


def metric(counter, label) -> float:
    return counter.labels(label)._value.get()


# ---------------------------------------------------------------------------
# /v1 and the kept /api routes
# ---------------------------------------------------------------------------
def test_v1_and_legacy_routes_answer_the_same(pipeline):
    client = TestClient(chat_app.app)
    new, old = ask(client), ask(client, path="/api/chat")
    assert new.status_code == old.status_code == 200
    assert new.json()["answer"] == old.json()["answer"] == "1 rows"
    assert "Deprecation" not in new.headers
    assert old.headers["Deprecation"] == "true" and "</v1/chat>" in old.headers["Link"]


def test_openapi_lists_v1_and_marks_legacy_deprecated():
    paths = chat_app.app.openapi()["paths"]
    assert not paths["/v1/chat"]["post"].get("deprecated") and not paths["/v1/health"]["get"].get("deprecated")
    assert paths["/api/chat"]["post"]["deprecated"] and paths["/api/health"]["get"]["deprecated"]
    params = {p["name"] for p in paths["/v1/chat"]["post"]["parameters"]}
    assert "Idempotency-Key" in params


def test_health_reports_cache_and_sqlgen_mode(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(chat_app.config, "REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setattr(chat_app.config, "SQLGEN_ADDR", "sqlgen:50051")
    body = TestClient(chat_app.app).get("/v1/health").json()
    assert body["cache"] == "redis" and body["sqlgen"] == "sqlgen:50051"


# ---------------------------------------------------------------------------
# answer cache in Redis, keyed by data version
# ---------------------------------------------------------------------------
def test_cache_hit_skips_the_model_and_the_query_and_lives_in_redis(pipeline, redis_store):
    client = TestClient(chat_app.app)
    first, second = ask(client, "Total sales by retailer"), ask(client, "  total SALES by   retailer ")
    assert not first.json()["cached"] and second.json()["cached"]
    assert pipeline["write_sql"] == 1 and pipeline["query"] == 1
    keys = redis_store.keys("chat:answer:*")
    assert len(keys) == 1 and keys[0].startswith("chat:answer:2026-10-18 06:00:00:")
    assert 0 < redis_store.ttl(keys[0]) <= cache.TTL


def test_cache_survives_a_restart_because_it_is_in_redis(pipeline):
    ask(TestClient(chat_app.app))
    cache.invalidate()                     # a new process: no in-memory state, same Redis
    assert ask(TestClient(chat_app.app)).json()["cached"]


def test_new_load_invalidates_cached_answers(pipeline):
    client = TestClient(chat_app.app)
    ask(client)
    pipeline["version"] = "2026-10-25 06:00:00"   # a new successful load
    cache.invalidate()                            # (or VERSION_TTL passes)
    assert not ask(client).json()["cached"] and pipeline["write_sql"] == 2


def test_version_is_looked_up_once_per_minute(pipeline, monkeypatch):
    lookups = []
    monkeypatch.setattr(chat_app.db, "data_version", lambda: lookups.append(1) or "v1")
    client = TestClient(chat_app.app)
    for q in ("Total sales by retailer", "Total sales by brand", "Total sales by retailer"):
        ask(client, q)
    assert len(lookups) == 1


def test_cached_answers_still_served_when_the_database_is_down(pipeline, monkeypatch):
    client = TestClient(chat_app.app)
    ask(client)
    monkeypatch.setattr(cache, "VERSION_TTL", 0)                 # force a fresh lookup...
    pipeline["version"] = RuntimeError("could not connect to server")   # ...which fails
    before = metric(telemetry.STORE_ERRORS, "data_version")
    assert ask(client).json()["cached"]                          # last known version still works
    assert metric(telemetry.STORE_ERRORS, "data_version") == before + 1


def test_redis_down_means_uncached_not_failed(pipeline):
    store.reset(DownRedis())
    client = TestClient(chat_app.app)
    before = metric(telemetry.STORE_ERRORS, "cache_get")
    first, second = ask(client), ask(client)
    assert first.status_code == second.status_code == 200 and not second.json()["cached"]
    assert pipeline["write_sql"] == 2 and metric(telemetry.STORE_ERRORS, "cache_get") == before + 2


# ---------------------------------------------------------------------------
# Idempotency-Key
# ---------------------------------------------------------------------------
def test_retry_with_the_same_key_replays_the_first_response(pipeline, monkeypatch):
    monkeypatch.setattr(cache, "get", lambda *a: None)   # the cache would hide a second run; turn it off
    client = TestClient(chat_app.app)
    first, retry = ask(client, key="order-42"), ask(client, key="order-42")
    assert first.status_code == retry.status_code == 200
    assert retry.json() == first.json() and retry.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert pipeline["write_sql"] == 1 and pipeline["query"] == 1


def test_without_a_key_every_request_runs(pipeline, monkeypatch):
    monkeypatch.setattr(cache, "get", lambda *a: None)
    client = TestClient(chat_app.app)
    ask(client), ask(client)
    assert pipeline["write_sql"] == 2


def test_same_key_different_body_is_rejected(pipeline):
    client = TestClient(chat_app.app)
    ask(client, "Total sales by retailer", key="k1")
    r = ask(client, "Total sales by brand", key="k1")
    assert r.status_code == 422 and "different request" in r.json()["detail"]


def test_same_key_while_the_first_is_running_is_409(pipeline, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_summary(*args):
        started.set()
        release.wait(5)
        return "done"

    monkeypatch.setattr(llm, "summarize", slow_summary)
    client = TestClient(chat_app.app)
    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", ask(client, key="k2")))
    first.start()
    assert started.wait(5)
    results["second"] = ask(client, key="k2")
    release.set()
    first.join(5)
    assert results["second"].status_code == 409 and results["first"].status_code == 200


def test_a_failed_first_run_releases_the_key(pipeline, monkeypatch):
    def busy(*args):
        raise llm.LLMUnavailable("throttled", kind="busy")

    monkeypatch.setattr(llm, "summarize", busy)
    client = TestClient(chat_app.app)
    assert ask(client, key="k3").status_code == 503
    monkeypatch.setattr(llm, "summarize", lambda *a: "recovered")
    retry = ask(client, key="k3")
    assert retry.status_code == 200 and retry.json()["answer"] == "recovered"
    assert "Idempotent-Replayed" not in retry.headers


def test_replay_through_the_legacy_route_keeps_deprecation_headers(pipeline):
    client = TestClient(chat_app.app)
    ask(client, key="k4", path="/api/chat")
    retry = ask(client, key="k4", path="/api/chat")
    assert retry.headers["Idempotent-Replayed"] == "true" and retry.headers["Deprecation"] == "true"


def test_key_record_expires(pipeline, redis_store):
    ask(TestClient(chat_app.app), key="k5")
    (record,) = redis_store.keys("chat:idem:*")
    assert idempotency.RESULT_TTL - 5 < redis_store.ttl(record) <= idempotency.RESULT_TTL
    assert "k5" not in record                               # stored hashed, not verbatim


@pytest.mark.parametrize("key", [b"x" * 256, "naïve".encode()])
def test_bad_keys_are_400(pipeline, key):
    r = TestClient(chat_app.app).post("/v1/chat", json={"message": "Total sales"}, headers={"Idempotency-Key": key})
    assert r.status_code == 400


def test_redis_down_runs_without_idempotency(pipeline):
    store.reset(DownRedis())
    before = metric(telemetry.STORE_ERRORS, "idempotency")
    r = ask(TestClient(chat_app.app), key="k6")
    assert r.status_code == 200 and metric(telemetry.STORE_ERRORS, "idempotency") >= before + 1


# ---------------------------------------------------------------------------
# sqlgen gRPC service
# ---------------------------------------------------------------------------
@pytest.fixture
def sqlgen(monkeypatch):
    """A real sqlgen server on a free port, the fake LLM behind it, the client pointed at it."""
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(llm.config, "FAKE_LLM_LATENCY_MS", 0)
    monkeypatch.setattr(llm.config, "FAKE_LLM_ERROR_RATE", 0)
    server, port = sqlgen_server.build("127.0.0.1:0", workers=4)
    server.start()
    monkeypatch.setattr(sqlgen_client.config, "SQLGEN_ADDR", f"127.0.0.1:{port}")
    monkeypatch.setattr(sqlgen_client.config, "SQLGEN_TIMEOUT_S", 5)
    sqlgen_client.reset()
    yield
    server.stop(None)
    sqlgen_client.reset()


def test_write_sql_over_grpc_matches_in_process(sqlgen, monkeypatch):
    views = ["chat.v_sales_by_week"]
    remote = sqlgen_client.write_sql("Total sales by retailer last week", views, "details")
    monkeypatch.setattr(sqlgen_client.config, "SQLGEN_ADDR", "")
    local = sqlgen_client.write_sql("Total sales by retailer last week", views, "details")
    assert remote == local and "chat.v_sales_by_week" in remote.sql


@pytest.mark.parametrize("kind", ["busy", "quota", "config", "declined"])
def test_llm_errors_cross_the_wire_with_their_kind(sqlgen, monkeypatch, kind):
    def fail(*args, **kwargs):
        raise llm.LLMUnavailable("model said no", kind=kind)

    monkeypatch.setattr(llm, "write_sql", fail)   # server side: same process, same module
    with pytest.raises(llm.LLMUnavailable) as exc:
        sqlgen_client.write_sql("q", ["chat.v_sales_by_week"], "d")
    assert exc.value.kind == kind and "model said no" in str(exc.value)


def test_unknown_views_are_rejected_by_the_service_as_a_bug(sqlgen):
    with pytest.raises(RuntimeError, match="INVALID_ARGUMENT"):
        sqlgen_client.write_sql("q", ["fact.fact_sales"], "d")


def test_unreachable_service_is_a_retryable_503(monkeypatch):
    monkeypatch.setattr(sqlgen_client.config, "SQLGEN_ADDR", "127.0.0.1:1")   # nothing listens there
    monkeypatch.setattr(sqlgen_client.config, "SQLGEN_TIMEOUT_S", 1)
    sqlgen_client.reset()
    with pytest.raises(llm.LLMUnavailable) as exc:
        sqlgen_client.write_sql("q", ["chat.v_sales_by_week"], "d")
    sqlgen_client.reset()
    assert exc.value.kind == "busy"


def test_full_chat_through_the_grpc_service(sqlgen, redis_store, monkeypatch):
    monkeypatch.setattr(chat_app.db, "query", lambda sql: {"columns": ["retailer"], "rows": [["Harbor Mart"]],
                                                           "row_count": 1})
    monkeypatch.setattr(chat_app.db, "known_values", lambda cols: {})
    monkeypatch.setattr(chat_app.db, "data_as_of", lambda: "2026-10-17")
    monkeypatch.setattr(chat_app.db, "data_version", lambda: "v1")
    r = ask(TestClient(chat_app.app), "Total sales by retailer last week")
    assert r.status_code == 200 and r.json()["sql"].startswith("SELECT") and r.json()["error"] is None
