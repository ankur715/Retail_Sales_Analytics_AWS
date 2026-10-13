"""Chatbot telemetry: golden-signal metrics on /metrics and one span per chat step."""
import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from chatbot import app as chat_app
from chatbot import llm, telemetry

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)     # once per process; telemetry.tracer is a proxy that picks it up


@pytest.fixture
def fake_chat(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(llm.config, "FAKE_LLM_LATENCY_MS", 0.0)
    monkeypatch.setattr(llm.config, "FAKE_LLM_ERROR_RATE", 0.0)
    monkeypatch.setattr(chat_app.db, "data_version", lambda: "2026-10-25 06:00:00")
    monkeypatch.setattr(chat_app.db, "known_values", lambda cols: {})
    monkeypatch.setattr(chat_app.db, "data_as_of", lambda: "2026-10-24")
    monkeypatch.setattr(chat_app.db.redshift, "get_connection", lambda **kw: FakeConn())
    EXPORTER.clear()


class FakeConn:
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.description = [("retailer",), ("sales_usd",)]

    def fetchall(self):
        return [("Northgate", 1.0)]

    def close(self):
        pass


def metric(text: str, line_start: str) -> float:
    return sum(float(l.rsplit(" ", 1)[1]) for l in text.splitlines() if l.startswith(line_start))


def test_one_span_per_step_and_no_question_text_in_spans(fake_chat):
    question = "Total sales by retailer last week"
    assert TestClient(chat_app.app).post("/api/chat", json={"message": question}).status_code == 200
    spans = EXPORTER.get_finished_spans()
    names = [s.name for s in spans]
    for step in ["llm.route", "db.known_values", "llm.write_sql", "sql.validate", "db.query", "db.data_as_of",
                 "llm.summarize"]:
        assert step in names
    assert {s.attributes.get("llm.model") for s in spans if s.name.startswith("llm.")} == {"fake"}
    assert next(s for s in spans if s.name == "db.query").attributes["db.rows"] == 1
    assert all(question not in str(dict(s.attributes)) for s in spans)


def test_metrics_count_requests_cache_and_step_latency(fake_chat):
    client = TestClient(chat_app.app)
    before = client.get("/metrics").text
    for _ in range(2):                                   # second one is a cache hit
        client.post("/api/chat", json={"message": "Total sales by retailer last week"})
    after = client.get("/metrics").text
    ok = 'chatbot_requests_total{route="/api/chat",status="200"}'
    assert metric(after, ok) - metric(before, ok) == 2
    assert metric(after, 'chatbot_cache_total{result="hit"}') - metric(before, 'chatbot_cache_total{result="hit"}') == 1
    assert metric(after, 'chatbot_step_seconds_count{step="llm.route"}') > 0
    assert metric(after, "chatbot_requests_in_flight") == 0
    assert "/metrics" not in after.split("chatbot_requests_total", 1)[1].split("# HELP", 1)[0]   # not self-counted


def test_llm_errors_count_as_503s_and_step_errors(fake_chat, monkeypatch):
    monkeypatch.setattr(llm.config, "FAKE_LLM_ERROR_RATE", 1.0)
    client = TestClient(chat_app.app)
    before = client.get("/metrics").text
    assert client.post("/api/chat", json={"message": "Total sales last week"}).status_code == 503
    after = client.get("/metrics").text
    bad = 'chatbot_requests_total{route="/api/chat",status="503"}'
    assert metric(after, bad) - metric(before, bad) == 1
    step = 'chatbot_step_errors_total{step="llm.route"}'
    assert metric(after, step) - metric(before, step) == 1
    assert any(s.name == "llm.route" and not s.status.is_ok for s in EXPORTER.get_finished_spans())
