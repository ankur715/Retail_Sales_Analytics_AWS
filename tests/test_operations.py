"""Milestone 3: load_completed events, database outages, the stale-answer fallback and gRPC
retries. No broker, no database, no model: the Kafka client, psycopg2 and the LLM are faked."""
import json
from datetime import date

import grpc
import psycopg2
import pytest
from fastapi.testclient import TestClient
from psycopg2.extensions import QueryCanceledError
from test_services import ask, pipeline, redis_store, sqlgen  # noqa: F401  (fixtures)

from chatbot import app as chat_app
from chatbot import cache, db, llm, telemetry
from chatbot import events as chat_events
from etl import events, steps
from sqlgen import client as sqlgen_client

CTX = steps.LoadContext(pipeline_id="C101_R201", client_id="C101", retailer_id="R201", product_level="serial",
                        source_type="s3", file_format="csv", load_id="L-42", load_type="automation",
                        window_start=date(2026, 9, 20), window_end=date(2026, 10, 18))


# ---------------------------------------------------------------------------
# publishing load_completed (etl/events.py)
# ---------------------------------------------------------------------------
class FakeProducer:
    def __init__(self, deliver=True, fail=False):
        self.deliver, self.fail, self.sent, self._cb = deliver, fail, [], None

    def produce(self, topic, key, value, on_delivery):
        if self.fail:
            raise RuntimeError("broker unreachable")
        self.sent.append((topic, key, json.loads(value)))
        self._cb = on_delivery

    def flush(self, timeout):
        if self._cb and self.deliver:
            self._cb(None, object())


def test_event_carries_the_load(monkeypatch):
    event = events.build(CTX)
    assert event["event"] == "load_completed" and event["version"] == 1
    assert (event["load_id"], event["pipeline_id"], event["window_end"]) == ("L-42", "C101_R201", "2026-10-18")
    assert event["finished_at"].endswith("+00:00")


def test_publish_is_off_without_a_broker(monkeypatch):
    monkeypatch.setattr(events.config, "EVENTS_BOOTSTRAP", "")
    monkeypatch.setattr(events, "producer", lambda: pytest.fail("must not connect"))
    assert events.publish_load_completed(CTX) is False


def test_publish_sends_keyed_by_pipeline_and_waits_for_the_ack(monkeypatch):
    fake = FakeProducer()
    monkeypatch.setattr(events.config, "EVENTS_BOOTSTRAP", "redpanda:9092")
    monkeypatch.setattr(events, "producer", lambda: fake)
    assert events.publish_load_completed(CTX) is True
    (topic, key, value), = fake.sent
    assert topic == "retail.load_completed" and key == "C101_R201" and value["load_id"] == "L-42"


@pytest.mark.parametrize("fake", [FakeProducer(fail=True), FakeProducer(deliver=False)])
def test_publish_never_raises(monkeypatch, fake):
    monkeypatch.setattr(events.config, "EVENTS_BOOTSTRAP", "redpanda:9092")
    monkeypatch.setattr(events, "producer", lambda: fake)
    assert events.publish_load_completed(CTX) is False


def test_finish_publishes_after_marking_success(monkeypatch):
    order = []
    monkeypatch.setattr(steps.redshift, "run", lambda stmts: order.append("SUCCESS") or [1])
    monkeypatch.setattr(events, "publish_load_completed", lambda ctx: order.append(("event", ctx.load_id)))
    steps.finish(CTX)
    assert order == ["SUCCESS", ("event", "L-42")]


# ---------------------------------------------------------------------------
# consuming it (chatbot/events.py)
# ---------------------------------------------------------------------------
def test_load_completed_retires_cached_answers(pipeline):   # noqa: F811
    client = TestClient(chat_app.app)
    ask(client)
    pipeline["version"] = "2026-10-25 06:00:00"            # the load landed...
    assert ask(client).json()["cached"]                     # ...but within the minute, the old answer
    assert chat_events.handle(json.dumps(events.build(CTX)).encode()) == "invalidated"
    assert not ask(client).json()["cached"]                 # the event retires it at once


@pytest.mark.parametrize("raw,result", [(b"not json", "malformed"), (b'{"event": "other"}', "ignored"),
                                        (b"[]", "ignored"), (None, "malformed")])
def test_other_messages_are_counted_not_acted_on(raw, result):
    assert chat_events.handle(raw) == result


class FakeMsg:
    def __init__(self, value, error=None):
        self._value, self._error = value, error

    def value(self):
        return self._value

    def error(self):
        return self._error


def test_listener_loop_handles_messages_and_survives_broker_errors(monkeypatch):
    from confluent_kafka import KafkaError
    msgs = [FakeMsg(None, error=KafkaError(KafkaError._TRANSPORT)), FakeMsg(json.dumps(events.build(CTX)).encode()),
            None]

    class FakeConsumer:
        def subscribe(self, topics):
            assert topics == ["retail.load_completed"]

        def poll(self, timeout):
            if not msgs:
                chat_events._stop.set()
                return None
            return msgs.pop(0)

        def close(self):
            pass

    before = telemetry.EVENTS.labels("load_completed", "invalidated")._value.get()
    monkeypatch.setattr(chat_events, "consumer", FakeConsumer)
    chat_events._stop.clear()
    chat_events._run()
    assert telemetry.EVENTS.labels("load_completed", "invalidated")._value.get() == before + 1


def test_missing_topic_before_the_first_load_is_not_a_broker_error():
    from confluent_kafka import KafkaError
    assert chat_events.classify(KafkaError(KafkaError.UNKNOWN_TOPIC_OR_PART)) == "topic_pending"
    assert chat_events.classify(KafkaError(KafkaError._TRANSPORT)) == "broker_error"


def test_listener_starts_only_with_a_broker(monkeypatch):
    monkeypatch.setattr(chat_events.config, "EVENTS_BOOTSTRAP", "")
    assert chat_events.start() is False


# ---------------------------------------------------------------------------
# database outages
# ---------------------------------------------------------------------------
def test_unreachable_database_is_database_unavailable(monkeypatch):
    def refuse(**kwargs):
        assert kwargs["attempts"] == 2 and kwargs["connect_timeout"] == 10   # fail fast: a person is waiting
        raise psycopg2.OperationalError("could not connect to server: Connection refused")

    monkeypatch.setattr(db.redshift, "get_connection", refuse)
    with pytest.raises(db.DatabaseUnavailable, match="Connection refused"):
        db.query("SELECT 1")


class BrokenCursor:
    def __init__(self, error):
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        if sql.startswith("SET"):
            return
        raise self.error


@pytest.mark.parametrize("error,expected", [
    (psycopg2.OperationalError("server closed the connection unexpectedly"), db.DatabaseUnavailable),
    (psycopg2.InterfaceError("connection already closed"), db.DatabaseUnavailable),
    (QueryCanceledError("canceling statement due to statement timeout"), QueryCanceledError),   # repairable
    (psycopg2.errors.UndefinedColumn("column x does not exist"), psycopg2.errors.UndefinedColumn),
])
def test_mid_query_failures_are_classified(monkeypatch, error, expected):
    class Conn:
        def cursor(self):
            return BrokenCursor(error)

        def close(self):
            pass

    monkeypatch.setattr(db.redshift, "get_connection", lambda **kw: Conn())
    with pytest.raises(expected):
        db.query("SELECT 1")


def test_database_down_is_a_clear_503_without_a_repair_round(pipeline, monkeypatch):   # noqa: F811
    def down(sql):
        raise db.DatabaseUnavailable("Connection refused")

    monkeypatch.setattr(chat_app.db, "query", down)
    r = ask(TestClient(chat_app.app))
    assert r.status_code == 503 and "database is unavailable" in r.json()["detail"]
    assert pipeline["write_sql"] == 1                       # no pointless repair call to the model


def test_failed_version_lookup_backs_off(pipeline, monkeypatch):   # noqa: F811
    lookups = []

    def failing():
        lookups.append(1)
        raise RuntimeError("down")

    ask(TestClient(chat_app.app))                            # learn a version
    monkeypatch.setattr(chat_app.db, "data_version", failing)
    monkeypatch.setattr(cache, "VERSION_TTL", 0)
    monkeypatch.setattr(cache, "VERSION_RETRY", 30)
    cache.data_version()                                     # fails, keeps the old version...
    for _ in range(5):
        assert cache.data_version() == "2026-10-18 06:00:00"   # ...and doesn't ask again for 30 s
    assert len(lookups) == 1


# ---------------------------------------------------------------------------
# stale-if-error fallback
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("outage", ["model", "database"])
def test_outage_serves_the_last_answer_marked_stale(pipeline, monkeypatch, outage):   # noqa: F811
    client = TestClient(chat_app.app)
    first = ask(client).json()
    pipeline["version"] = "2026-10-25 06:00:00"             # new data, so the cached answer no longer matches
    cache.invalidate()
    if outage == "model":
        def busy(*a, **k):
            raise llm.LLMUnavailable("throttled", kind="busy")
        monkeypatch.setattr(llm, "route", busy)
    else:
        def down(sql):
            raise db.DatabaseUnavailable("Connection refused")
        monkeypatch.setattr(chat_app.db, "query", down)
    r = ask(client)
    assert r.status_code == 200 and r.json()["stale"] and r.json()["cached"]
    assert r.json()["answer"] == first["answer"] and r.json()["data_as_of"] == first["data_as_of"]


def test_outage_without_a_previous_answer_is_still_503(pipeline, monkeypatch):   # noqa: F811
    def busy(*a, **k):
        raise llm.LLMUnavailable("throttled", kind="busy")
    monkeypatch.setattr(llm, "route", busy)
    assert ask(TestClient(chat_app.app), "A question nobody asked before").status_code == 503


def test_stale_answers_are_not_stored_as_the_idempotent_result(pipeline, monkeypatch):   # noqa: F811
    client = TestClient(chat_app.app)
    ask(client)
    pipeline["version"] = "v2"
    cache.invalidate()

    def busy(*a, **k):
        raise llm.LLMUnavailable("throttled", kind="busy")
    real_route = llm.route
    monkeypatch.setattr(llm, "route", busy)
    assert ask(client, key="retry-me").json()["stale"]
    monkeypatch.setattr(llm, "route", real_route)           # the model is back
    retry = ask(client, key="retry-me")
    assert not retry.json()["stale"] and "Idempotent-Replayed" not in retry.headers


# ---------------------------------------------------------------------------
# gRPC retries
# ---------------------------------------------------------------------------
def test_a_transient_unavailable_is_retried_transparently(sqlgen, monkeypatch):   # noqa: F811
    calls = []
    real = llm.write_sql

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise llm.LLMUnavailable("pod going away", kind="busy")   # -> UNAVAILABLE on the wire
        return real(*args, **kwargs)

    monkeypatch.setattr(llm, "write_sql", flaky)
    plan = sqlgen_client.write_sql("Total sales by retailer last week", ["chat.v_sales_by_week"], "d")
    assert plan.sql and len(calls) == 2


def test_quota_errors_are_not_retried(sqlgen, monkeypatch):   # noqa: F811
    calls = []

    def quota(*args, **kwargs):
        calls.append(1)
        raise llm.LLMUnavailable("daily quota", kind="quota")

    monkeypatch.setattr(llm, "write_sql", quota)
    with pytest.raises(llm.LLMUnavailable):
        sqlgen_client.write_sql("q", ["chat.v_sales_by_week"], "d")
    assert len(calls) == 1


def test_channel_retries_unavailable_and_reconnects_fast():
    policy = json.loads(sqlgen_client.SERVICE_CONFIG)
    assert policy["methodConfig"][0]["retryPolicy"]["retryableStatusCodes"] == ["UNAVAILABLE"]
    assert grpc.StatusCode.UNAVAILABLE in sqlgen_client.KINDS
    options = dict(sqlgen_client.CHANNEL_OPTIONS)
    assert options["grpc.max_reconnect_backoff_ms"] <= 1000      # not gRPC's 2 min
    assert options["grpc.min_reconnect_backoff_ms"] <= 1000      # = the connect timeout; default 20 s
