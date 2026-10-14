"""Fake LLM provider (LLM_PROVIDER=fake): the chatbot's full path with no model and no cost."""
import pytest
from fastapi.testclient import TestClient

from chatbot import app as chat_app
from chatbot import catalog, guard, llm, llm_fake
from triage import agent


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(llm.config, "FAKE_LLM_LATENCY_MS", 0.0)
    monkeypatch.setattr(llm.config, "FAKE_LLM_ERROR_RATE", 0.0)


@pytest.fixture
def no_db(monkeypatch):
    queries = []
    monkeypatch.setattr(chat_app.db, "data_version", lambda: "2026-10-25 06:00:00")
    monkeypatch.setattr(chat_app.db, "known_values", lambda cols: {})
    monkeypatch.setattr(chat_app.db, "data_as_of", lambda: "2026-10-24")

    def query(sql):
        queries.append(sql)
        return {"columns": ["retailer", "sales_usd"], "rows": [["Harbor Mart", 1.0], ["Northgate", 2.0]], "row_count": 2}

    monkeypatch.setattr(chat_app.db, "query", query)
    return queries


@pytest.mark.parametrize("question, view", [
    ("Total sales by retailer last week", "chat.v_sales_by_week"),
    ("Which products were out of stock?", "chat.v_weekly_sales"),
    ("Top 5 styles at Harbor Mart", "chat.v_weekly_sales"),
])
def test_routes_by_keyword(question, view):
    assert llm.route(question).views == [view]


@pytest.mark.parametrize("view", catalog.view_names())
def test_canned_sql_passes_the_guard_for_every_view(view):
    plan = llm.write_sql("anything", [view], details="")
    guard.validate(plan.sql, [view])      # raises if the canned SQL reaches outside the routed view


def test_full_chat_path_with_the_fake_model(no_db):
    r = TestClient(chat_app.app).post("/api/chat", json={"message": "Total sales by retailer last week"}).json()
    assert r["error"] is None and r["views"] == ["chat.v_sales_by_week"]
    assert r["answer"] == "(fake answer) The query returned 2 rows." and len(no_db) == 1
    assert TestClient(chat_app.app).get("/api/health").json()["model"] == "fake"


def test_error_rate_one_gives_the_busy_503(no_db, monkeypatch):
    monkeypatch.setattr(llm.config, "FAKE_LLM_ERROR_RATE", 1.0)
    r = TestClient(chat_app.app).post("/api/chat", json={"message": "Total sales last week"})
    assert r.status_code == 503 and no_db == []


def test_latency_sleeps_per_call(monkeypatch):
    slept = []
    monkeypatch.setattr(llm.config, "FAKE_LLM_LATENCY_MS", 200.0)
    monkeypatch.setattr(llm_fake.time, "sleep", slept.append)
    llm.route("Total sales last week")
    llm.summarize("q", "SELECT 1", ["n"], [[1]], None)
    assert len(slept) == 2 and all(s >= 0 for s in slept)


def test_fake_provider_never_turns_the_triage_agent_on():
    assert llm.configured() and not agent.enabled()     # triage would call real Bedrock


def test_by_brand_questions_filter_to_the_named_brand():
    from chatbot.llm_fake import _by_brand
    assert "brand = 'Cobalt Outdoor'" in _by_brand("Sales by brand for Cobalt Outdoor last week")
    assert "brand =" not in _by_brand("Sales by brand for every brand")
    guard.validate(_by_brand("Sales by brand for Aurora Apparel"), ["chat.v_sales_by_week"])

