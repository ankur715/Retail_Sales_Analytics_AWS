"""What goes in the index: one Chunk per retrievable piece of knowledge."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from chatbot import access, catalog

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_PATH = ROOT / "config" / "sql_examples.yaml"
SAMPLE_NOTES_PATH = ROOT / "config" / "triage_notes_sample.jsonl"


@dataclass
class Chunk:
    id: str
    kind: str                  # example | view | rule | triage_note
    title: str
    body: str                  # what the model is shown
    embed_text: str            # what is embedded (examples: just the question -- they're matched on it)
    views: list[str] = field(default_factory=list)   # views the chunk is about / its SQL uses
    brand: str | None = None   # set for brand-specific chunks (triage notes): access-filtered
    meta: dict = field(default_factory=dict)


def examples() -> list[Chunk]:
    out = []
    for name, view in catalog.load()["views"].items():
        for i, ex in enumerate(view.get("examples", [])):
            used = sorted(set(re.findall(r"chat\.v_\w+", ex["sql"])))
            out.append(_example(f"catalog:{name}:{i}", ex["question"], ex["sql"], used))
    for i, ex in enumerate(yaml.safe_load(EXAMPLES_PATH.read_text())["examples"]):
        out.append(_example(f"bank:{i}", ex["question"], ex["sql"], sorted(ex["views"])))
    return out


def _example(cid: str, question: str, sql: str, views: list[str]) -> Chunk:
    return Chunk(id=cid, kind="example", title=question, body=f"Question: {question}\nSQL: {sql}",
                 embed_text=question, views=views, meta={"sql": sql})


def view_docs() -> list[Chunk]:
    out = []
    for name, v in catalog.load()["views"].items():
        cols = "; ".join(f"{c}: {d}" for c, d in v["columns"].items())
        body = f"View {name} ({v['grain']}). {v['summary']} Columns: {cols}"
        out.append(Chunk(id=f"view:{name}", kind="view", title=name, body=body, embed_text=body, views=[name]))
    return out


def rules() -> list[Chunk]:
    return [Chunk(id=f"rule:{i}", kind="rule", title=f"business rule {i + 1}", body=r, embed_text=r)
            for i, r in enumerate(catalog.load()["business_rules"])]


def triage_notes(rows: list[dict] | None = None) -> list[Chunk]:
    """Past triage-agent notes. Default: the synthetic sample (the Postgres copy has no
    etl.load_audit); pass rows exported from Redshift for the real ones."""
    if rows is None:
        rows = [json.loads(line) for line in SAMPLE_NOTES_PATH.read_text().splitlines() if line.strip()]
    brands = access.client_brands()
    out = []
    for r in rows:
        brand = brands.get(r["client_id"])
        title = f"Triage note for {r['pipeline_id']} ({brand}), failed at {r['failed_task']} on {r['created_at'][:10]}"
        out.append(Chunk(id=f"triage:{r['load_id']}", kind="triage_note", title=title,
                         body=f"{title}:\n{r['note']}", embed_text=f"{title}. {r['note']}", brand=brand,
                         views=["chat.v_data_freshness"], meta={"synthetic": bool(r.get("synthetic"))}))
    return out


def all_chunks(notes: list[dict] | None = None) -> list[Chunk]:
    return examples() + view_docs() + rules() + triage_notes(notes)
