"""Milestone 5: token/cost accounting and retrieval. No database, no model: the pgvector store
is faked here; tests/test_rag_db.py runs the real one against Postgres (CI job ai-integration)."""
import re

import pytest
import yaml
from fastapi.testclient import TestClient
from test_services import ask, pipeline, redis_store, sqlgen  # noqa: F401  (fixtures)

from chatbot import access, catalog, llm, usage
from chatbot import app as chat_app
from rag import documents, embed, retrieve
from sqlgen import client as sqlgen_client


# ---------------------------------------------------------------------------
# prices and usage
# ---------------------------------------------------------------------------
def test_prices_come_from_the_reviewed_table():
    assert usage.price("us.anthropic.claude-haiku-4-5-20251001-v1:0") == (1.0, 5.0)
    assert usage.price("us.amazon.nova-lite-v1:0") == (0.06, 0.24)
    assert usage.price("fake") == (1.0, 5.0)                     # priced as the default real model
    assert usage.price("some-new-model") is None                 # never guessed
    assert usage.cost("some-new-model", 1000, 1000) == 0.0
    assert usage.cost("claude-haiku-4-5", 1_000_000, 100_000) == pytest.approx(1.5)


def test_usage_adds_up_per_question_and_by_step():
    before = usage.TOKENS.labels("bedrock", "claude-haiku-4-5", "route", "input")._value.get()
    with usage.track() as used:
        with usage.step("route"):
            usage.record("bedrock", "claude-haiku-4-5", 2000, 100)
        usage.add(500, 50, 0.001)                                # e.g. reported back by sqlgen
    assert (used.llm_calls, used.input_tokens, used.output_tokens) == (2, 2500, 150)
    assert used.cost_usd == pytest.approx(0.0025 + 0.001)
    assert usage.TOKENS.labels("bedrock", "claude-haiku-4-5", "route", "input")._value.get() == before + 2000


def test_outside_a_question_nothing_accumulates():
    usage.record("fake", "fake", 10, 10)                        # metrics only; no error


def test_each_response_reports_its_own_cost(pipeline, monkeypatch):   # noqa: F811
    def summarize(*args, **kwargs):              # one model call: 1,000 tokens in, 100 out, at Haiku prices
        usage.record("bedrock", "claude-haiku-4-5", 1000, 100)
        return "summary"

    monkeypatch.setattr(llm, "summarize", summarize)
    client = TestClient(chat_app.app)
    first, again = ask(client).json(), ask(client).json()
    assert first["usage"] == {"llm_calls": 1, "input_tokens": 1000, "output_tokens": 100, "cost_usd": 0.0015}
    assert again["cached"] and again["usage"]["llm_calls"] == 0 and again["usage"]["cost_usd"] == 0.0


def test_sqlgen_reports_its_usage_back_over_grpc(sqlgen):   # noqa: F811
    with usage.track() as used:
        sqlgen_client.write_sql("Total sales by retailer last week", ["chat.v_sales_by_week"], "details")
    assert used.llm_calls == 1 and used.input_tokens > 0 and used.cost_usd > 0


# ---------------------------------------------------------------------------
# documents and embeddings
# ---------------------------------------------------------------------------
def test_corpus_has_examples_views_rules_and_notes():
    chunks = documents.all_chunks()
    kinds = {k: sum(c.kind == k for c in chunks) for k in ("example", "view", "rule", "triage_note")}
    assert kinds == {"example": 30, "view": 4, "rule": 7, "triage_note": 6}
    assert len({c.id for c in chunks}) == len(chunks)


def test_every_example_declares_exactly_the_views_it_uses_and_passes_the_guard():
    from chatbot import guard
    for c in documents.examples():
        assert c.views == sorted(set(re.findall(r"chat\.v_\w+", c.meta["sql"])))
        guard.validate(c.meta["sql"], c.views)


def test_triage_notes_are_tagged_with_their_brand():
    notes = documents.triage_notes()
    assert {n.brand for n in notes} <= set(access.client_brands().values())
    assert all(n.meta["synthetic"] for n in notes)


def test_hash_embeddings_are_normalised_and_deterministic():
    e = embed.get("hash")
    a, b = e.query("sales by retailer"), e.query("sales by retailer")
    assert a == b and len(a) == embed.DIM and abs(sum(x * x for x in a) - 1) < 1e-9


def test_hash_embeddings_rank_word_overlap_first():
    e = embed.get("hash")
    q = e.query("best selling color for Cobalt Outdoor")
    docs = e.documents(["Best-selling color for Aurora Apparel over the last 4 weeks",
                        "When was Bramble Footwear's data last loaded?"])
    sims = [sum(x * y for x, y in zip(q, d)) for d in docs]
    assert sims[0] > sims[1]


def test_examples_dont_overlap_the_eval_set():
    path = documents.ROOT / "evals" / "questions.yaml"
    evals = {q["question"].lower() for q in yaml.safe_load(path.read_text())["questions"]}
    assert not evals & {c.title.lower() for c in documents.examples()}


# ---------------------------------------------------------------------------
# retrieval in the chatbot
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_index(monkeypatch):
    """retrieve() against a recording fake store."""
    calls = []
    monkeypatch.setattr(retrieve.config, "RAG_ENABLED", True)
    monkeypatch.setattr(retrieve, "_connection", lambda: object())

    def search(conn, vector, kind, k, views=None, brands=None):
        calls.append({"kind": kind, "k": k, "views": views, "brands": brands})
        if kind == "example":
            return [{"title": "Sales by size for Bramble Footwear last week", "similarity": 0.81,
                     "meta": {"sql": "SELECT size FROM chat.v_weekly_sales"}}]
        return [{"body": "Triage note for C103_R203 (Cobalt Outdoor): file late", "similarity": 0.5, "meta": {}}]

    monkeypatch.setattr(retrieve.store, "search", search)
    return calls


def test_retrieval_filters_examples_by_routed_views_and_notes_by_brand(fake_index):
    who = access.Principal("u", brands=frozenset({"Cobalt Outdoor"}))
    got = retrieve.retrieve("is Harbor Mart late?", ["chat.v_data_freshness"], who)
    assert fake_index[0] == {"kind": "example", "k": 4, "views": ["chat.v_data_freshness"], "brands": None}
    assert fake_index[1]["kind"] == "triage_note" and fake_index[1]["brands"] == {"Cobalt Outdoor"}
    assert got.examples[0]["sql"].startswith("SELECT") and got.notes


def test_notes_only_for_freshness_questions(fake_index):
    retrieve.retrieve("sales by size", ["chat.v_weekly_sales"], access.ANONYMOUS)
    assert [c["kind"] for c in fake_index] == ["example"]


def test_retrieval_failure_falls_back_quietly(monkeypatch):
    monkeypatch.setattr(retrieve.config, "RAG_ENABLED", True)

    def down():
        raise ConnectionError("pgvector down")

    monkeypatch.setattr(retrieve, "_connection", down)
    before = retrieve.ERRORS.labels("ConnectionError")._value.get()
    assert retrieve.retrieve("q", ["chat.v_sales_by_week"], access.ANONYMOUS) is None
    assert retrieve.ERRORS.labels("ConnectionError")._value.get() == before + 1


def test_retrieved_examples_replace_the_catalogs_in_the_prompt(pipeline, fake_index, monkeypatch):   # noqa: F811
    seen = {}

    def write_sql(question, views, details, **kw):
        seen["details"] = details
        return llm.SqlPlan(sql="SELECT brand FROM chat.v_sales_by_week", explanation="x")

    monkeypatch.setattr(llm, "write_sql", write_sql)
    ask(TestClient(chat_app.app))
    assert "Similar questions already answered correctly" in seen["details"]
    assert "Sales by size for Bramble Footwear" in seen["details"]
    catalog_example = catalog.load()["views"]["chat.v_sales_by_week"]["examples"][0]["question"]
    assert catalog_example not in seen["details"]


def test_summary_prompt_carries_notes_only_when_given(monkeypatch):
    prompts = []
    monkeypatch.setattr(llm, "provider", lambda: type("P", (), {
        "text_call": staticmethod(lambda p: prompts.append(p) or "ok"), "model_name": staticmethod(lambda: "x")}))
    llm.summarize("q", "SELECT 1", ["a"], [[1]], "2026-10-17")
    llm.summarize("q", "SELECT 1", ["a"], [[1]], "2026-10-17", notes=["Triage note: file late"])
    assert "triage agent" not in prompts[0] and "Triage note: file late" in prompts[1]


def test_nested_tracking_rolls_up():
    with usage.track() as outer:
        with usage.track() as inner:
            usage.record("fake", "fake", 100, 10)
        usage.record("fake", "fake", 50, 5)
    assert inner.llm_calls == 1 and outer.llm_calls == 2 and outer.input_tokens == 150


# ---------------------------------------------------------------------------
# eval scoring
# ---------------------------------------------------------------------------
def test_result_match_allows_extra_columns_order_and_rounding():
    from evals.run import result_matches
    gold = [["Aurora Apparel", 1234.5], ["Cobalt Outdoor", 99.99]]
    assert result_matches([[99.99, "Cobalt Outdoor", 7], [1234.49, "AURORA APPAREL", 3]], gold)
    assert not result_matches([["Aurora Apparel", 1234.5]], gold)                     # a row missing
    assert not result_matches([["Aurora Apparel", 1300.0], ["Cobalt Outdoor", 99.99]], gold)   # wrong number
    assert result_matches([], [])


def test_judge_prompt_shows_the_reference_only_for_answerable_questions():
    from evals.run import judge_prompt
    answer = judge_prompt({"question": "q", "expect": "answer"}, {"sql": "S", "answer": "A"},
                          {"columns": ["x"], "rows": [[1]]})
    refuse = judge_prompt({"question": "q", "expect": "refuse"}, {"answer": "no"}, None)
    assert "Reference result" in answer and "no valid answer" in refuse


# ---------------------------------------------------------------------------
# Ollama provider (HTTP mocked)
# ---------------------------------------------------------------------------
class FakeHTTP:
    def __init__(self, status=200, content='{"views": ["chat.v_sales_by_week"], "reason": "totals"}'):
        self.status_code, self.content, self.sent = status, content, []

    def __call__(self, url, json, timeout):
        self.sent.append((url, json))
        from types import SimpleNamespace
        return SimpleNamespace(status_code=self.status_code, json=lambda: {
            "message": {"content": self.content}, "prompt_eval_count": 120, "eval_count": 15})


@pytest.fixture
def ollama(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(llm.config, "OLLAMA_URL", "http://ollama:11434")
    fake = FakeHTTP()
    monkeypatch.setattr("requests.post", fake)
    return fake


def test_ollama_structured_call_sends_the_schema_and_counts_tokens(ollama):
    with usage.track() as used:
        route = llm.route("Total sales by retailer last week")
    assert route.views == ["chat.v_sales_by_week"]
    url, body = ollama.sent[0]
    assert url == "http://ollama:11434/api/chat" and body["format"]["title"] == "Route"
    assert (used.llm_calls, used.input_tokens, used.output_tokens, used.cost_usd) == (1, 120, 15, 0.0)
    assert llm.configured() and llm.model_name() == "ollama/qwen2.5:1.5b"


def test_ollama_missing_model_is_a_config_error(ollama):
    ollama.status_code = 404
    with pytest.raises(llm.LLMUnavailable) as e:
        llm.route("q")
    assert e.value.kind == "config"


def test_ollama_malformed_output_is_retryable(ollama):
    ollama.content = "not json"
    with pytest.raises(llm.LLMUnavailable) as e:
        llm.route("q")
    assert e.value.kind == "busy"
