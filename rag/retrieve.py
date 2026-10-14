"""Per-question retrieval for the chatbot. Never fails a request: if the index is down or was
built by a different embedder, the chatbot falls back to the catalog's own examples (as before
milestone 5) and counts the error (chatbot_rag_errors_total)."""
import logging
import re
import threading
from dataclasses import dataclass, field

from prometheus_client import Counter

from chatbot import access, telemetry
from etl import config
from rag import embed, store

log = logging.getLogger(__name__)
ERRORS = Counter("chatbot_rag_errors_total", "Retrievals that failed and fell back to catalog examples", ["reason"])
_conn = None
_lock = threading.Lock()
# Triage notes only help when the question is about a problem. The first real-model eval showed
# Haiku repeating unrelated notes as facts in plain "when was the data loaded" answers.
PROBLEM_WORDS = re.compile(r"\b(why|late|behind|missing|stale|fail\w*|error\w*|problem\w*|issue\w*|delay\w*|"
                           r"broken|wrong|stuck|gap\w*)\b", re.IGNORECASE)


@dataclass
class Retrieved:
    examples: list[dict] = field(default_factory=list)   # {"question", "sql", "similarity"}
    notes: list[str] = field(default_factory=list)       # triage-note bodies


def _connection():
    global _conn
    with _lock:
        if _conn is None or _conn.closed:
            _conn = store.connect(config.RAG_DATABASE_URL)
            _conn.autocommit = True
            built_by = store.embedder_name(_conn)
            if built_by != config.RAG_EMBEDDER:
                _conn.close()
                _conn = None
                raise RuntimeError(f"index built by {built_by!r}, chatbot uses {config.RAG_EMBEDDER!r}: re-run rag.ingest")
        return _conn


def retrieve(question: str, views: list[str], who: access.Principal) -> Retrieved | None:
    if not config.RAG_ENABLED:
        return None
    global _conn
    try:
        with telemetry.step("rag.retrieve", **{"rag.embedder": config.RAG_EMBEDDER}) as span:
            vector = embed.get(config.RAG_EMBEDDER).query(question)
            conn = _connection()
            examples = store.search(conn, vector, "example", config.RAG_EXAMPLES_K, views=views)
            notes = []
            if "chat.v_data_freshness" in views and PROBLEM_WORDS.search(question):   # "why is it late?"
                notes = store.search(conn, vector, "triage_note", config.RAG_NOTES_K, brands=who.brands)
            span.set_attribute("rag.examples", len(examples))
            span.set_attribute("rag.notes", len(notes))
    except Exception as e:
        ERRORS.labels(type(e).__name__).inc()
        log.warning("retrieval failed (%s); using catalog examples", e)
        with _lock:
            if _conn is not None:
                _conn.close()
            _conn = None
        return None
    return Retrieved(examples=[{"question": r["title"], "sql": r["meta"]["sql"], "similarity": round(r["similarity"], 3)}
                               for r in examples],
                     notes=[r["body"] for r in notes])
