"""Milestone 5 against real Postgres + pgvector: every example and reference query runs, the
index builds and retrieves sensibly with access filters, and the eval harness runs end to end."""
import json
import os

import pytest
import yaml

from chatbot import access, db, guard
from rag import documents, embed, store
ADMIN, READER = os.environ.get("PG_TEST_DSN"), os.environ.get("PG_READER_DSN")

pytestmark = pytest.mark.skipif(not (ADMIN and READER), reason="needs PG_TEST_DSN and PG_READER_DSN")
EVAL = yaml.safe_load((documents.ROOT / "evals" / "questions.yaml").read_text())["questions"]
ANSWERABLE = [q for q in EVAL if q["expect"] == "answer"]


@pytest.mark.parametrize("chunk", documents.examples(), ids=lambda c: c.id)
def test_every_example_runs_as_chat_reader(chunk):
    result = db.query(guard.validate(chunk.meta["sql"], chunk.views))
    assert result["columns"]


@pytest.mark.parametrize("item", ANSWERABLE, ids=lambda q: q["id"])
def test_every_reference_query_runs(item):
    db.query(item["reference_sql"])


def test_reference_results_are_mostly_non_empty():
    """An empty reference makes a question trivially easy; a few are fine (e.g. "is anything out
    of stock" can genuinely be no), most must return data."""
    empty = [q["id"] for q in ANSWERABLE if not db.query(q["reference_sql"])["rows"]]
    assert len(empty) <= 3, empty


@pytest.fixture(scope="module")
def index():
    conn = store.connect(ADMIN)
    n = store.rebuild(conn, documents.all_chunks(), embed.get("hash"))
    conn.close()
    reader = store.connect(READER)                      # the chatbot's read-only user
    yield reader, n
    reader.close()


def test_index_builds_and_records_its_embedder(index):
    conn, n = index
    assert n == len(documents.all_chunks()) and store.embedder_name(conn) == "hash"


def test_nearest_example_shares_the_question_words(index):
    conn, _ = index
    vec = embed.get("hash").query("Which color sells best for Bramble Footwear over the last 4 weeks?")
    top = store.search(conn, vec, "example", 1, views=["chat.v_weekly_sales"])
    assert "color" in top[0]["title"].lower()


def test_examples_are_limited_to_the_routed_views(index):
    conn, _ = index
    vec = embed.get("hash").query("sales in September by brand")
    for r in store.search(conn, vec, "example", 10, views=["chat.v_sales_by_week"]):
        assert set(r["views"]) <= {"chat.v_sales_by_week"}       # never the calendar-join examples


def test_triage_notes_respect_brand_access(index):
    conn, _ = index
    vec = embed.get("hash").query("why is the data late at Harbor Mart?")
    cobalt = store.search(conn, vec, "triage_note", 10, brands=frozenset({"Cobalt Outdoor"}))
    assert cobalt and {r["brand"] for r in cobalt} == {"Cobalt Outdoor"}
    everyone = store.search(conn, vec, "triage_note", 10, brands=None)
    assert {r["brand"] for r in everyone} == set(access.client_brands().values())


def test_reader_cannot_change_the_index(index):
    import psycopg2
    conn, _ = index
    with pytest.raises(psycopg2.Error), conn.cursor() as cur:
        cur.execute("DELETE FROM rag.chunks")
    conn.rollback()


def test_eval_harness_runs_end_to_end_with_the_fake_model(index, monkeypatch, tmp_path):
    """The real pipeline + database, the fake model and fake judge: proves the harness, routing,
    the 403 path and the refusal path work. (Answer quality needs a real model: README.)"""
    from chatbot import llm
    from evals import run
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(llm.config, "FAKE_LLM_LATENCY_MS", 0)
    monkeypatch.setattr(llm.config, "FAKE_LLM_ERROR_RATE", 0)
    monkeypatch.setattr(run, "RESULTS", tmp_path)
    monkeypatch.setattr(run, "SCORECARD", tmp_path / "scorecard.svg")
    assert run.main(["--judge", "fake", "--only", "tot-02", "fresh-02", "refuse-01", "adv-03", "adv-01"]) == 0
    results = {r["id"]: r for r in json.loads(next(tmp_path.glob("eval_*.json")).read_text())["results"]}
    assert results["adv-03"]["passed"] and results["adv-03"]["status"] in (200, 403)
    assert results["refuse-01"]["passed"] and results["adv-01"]["executed_ok"]
    assert all(r["usage"]["llm_calls"] >= 0 for r in results.values())
    assert (tmp_path / "scorecard.svg").exists()
