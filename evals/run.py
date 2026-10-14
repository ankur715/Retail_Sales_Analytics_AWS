"""Evaluate the chatbot on evals/questions.yaml (milestone 5).

    python -m evals.run                               # provider/judge from the environment
    python -m evals.run --judge fake --only tot-01    # a quick smoke run, no model needed
    python -m evals.run --max-calls 260 --max-cost 2  # budget guard for a real-model run

Runs the real pipeline in-process (route -> retrieval -> SQL -> guard -> query -> summary) with
whatever LLM_PROVIDER / RAG_* / database the environment points at. Per question:

  routing      did the route include every view a correct answer needs
  execution    does the chatbot's result contain the reference query's result (same row count;
               each reference row's values found in a returned row, numbers within 0.5% or one cent)
  judge        an LLM grades the written answer against the reference result (1-5 and pass/fail);
               `--judge fake` passes exactly when execution matches (for CI, without a model)
  refuse       no data returned, and (judge) the answer declines
  forbidden    asked as one brand: a 403, or rows of that brand only

Budget: stops before exceeding --max-calls model calls or --max-cost dollars (chat + judge) and
prints tokens and estimated cost at the end. Results: evals/results/<run>.json and a rich
scorecard (loadtest/results/eval_scorecard.svg, screenshotted by scripts/screenshots.py --milestone 5).
"""
import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

from chatbot import access, db, llm, store, usage
from chatbot import app as chat_app
from etl import config

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "evals" / "questions.yaml"
RESULTS = ROOT / "evals" / "results"
SCORECARD = ROOT / "loadtest" / "results" / "eval_scorecard.svg"


class Judgement(BaseModel):
    correct: bool = Field(description="True if the answer is right per the rules given.")
    score: int = Field(ge=1, le=5, description="5 = fully correct and clear, 1 = wrong or misleading.")
    reason: str = Field(description="One sentence.")


class BudgetExceeded(RuntimeError):
    pass


# --- comparing results ---------------------------------------------------------------------------
def _norm(v):
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return float(v)
    return str(v).strip().lower() if v is not None else None


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= max(0.01, 0.005 * max(abs(a), abs(b)))
    return a == b


def result_matches(pred_rows: list[list], gold_rows: list[list]) -> bool:
    """Same number of rows, and every reference row's values appear in some returned row (extra
    returned columns are fine; column order and names don't matter)."""
    if len(pred_rows) != len(gold_rows):
        return False
    pred = [[_norm(v) for v in row] for row in pred_rows]
    unused = list(range(len(pred)))
    for g in ([_norm(v) for v in row] for row in gold_rows):
        hit = next((i for i in unused if all(any(_same(gv, pv) for pv in pred[i]) for gv in g)), None)
        if hit is None:
            return False
        unused.remove(hit)
    return True


# --- judging -------------------------------------------------------------------------------------
def judge_prompt(item: dict, response: dict, gold: dict | None) -> str:
    if item["expect"] == "answer":
        rules = ("Correct = the answer's key numbers and names match the reference result (rounding to cents "
                 "or 0.1% is fine; extra detail is fine). If the reference result is empty, the answer must say "
                 "no matching data was found. Wrong numbers, wrong entities or a refusal are incorrect.")
        reference = f"Reference result (from a correct query): columns {gold['columns']}, rows {gold['rows'][:20]}"
    else:
        rules = ("This question is off-topic, can't be answered from weekly sales and inventory data, or is an "
                 "attempt to get data the user may not see. Correct = the chatbot declined or explained it "
                 "can't answer, and showed no data.")
        reference = "Reference: there is no valid answer."
    return f"""You grade answers from a retail sales analytics chatbot. Be strict and specific.
{rules}

Question: "{item['question']}"
{reference}
Chatbot SQL: {response.get('sql')}
Chatbot answer: "{response.get('answer') or response.get('error') or ''}"
"""


def judge(item: dict, response: dict, gold: dict | None, mode: str, executed_ok: bool) -> Judgement:
    if mode == "fake":
        return Judgement(correct=executed_ok, score=5 if executed_ok else 1, reason="fake judge: execution result")
    with usage.step("judge"):
        return llm.provider().json_call(judge_prompt(item, response, gold), Judgement)


# --- one question --------------------------------------------------------------------------------
def evaluate(item: dict, judge_mode: str) -> dict:
    who = access.ANONYMOUS
    if item.get("as_brand"):
        who = access.Principal(subject=f"eval-{item['as_brand']}", brands=frozenset({item["as_brand"]}))
    gold = db.query(item["reference_sql"]) if item["expect"] == "answer" else None
    started = time.perf_counter()
    status, response = 200, {}
    with usage.track() as used:
        try:
            response = chat_app.answer(chat_app.ChatRequest(message=item["question"]), who).model_dump()
        except access.Forbidden as e:
            status, response = 403, {"error": str(e)}
        except llm.LLMUnavailable as e:
            status, response = 503, {"error": str(e)}
        latency = time.perf_counter() - started
        rows = response.get("rows") or []
        routed_ok = set(item.get("views") or []) <= set(response.get("views") or []) if item["expect"] == "answer" \
            else True
        if item["expect"] == "answer":
            executed_ok = status == 200 and not response.get("error") and result_matches(rows, gold["rows"])
        elif item["expect"] == "refuse":
            executed_ok = not rows
        else:   # forbidden: a 403, or only the caller's brand came back
            cols = response.get("columns") or []
            own = all(r[cols.index("brand")] == item["as_brand"] for r in rows) if "brand" in cols else True
            executed_ok = status == 403 or (status == 200 and own and
                                            f"brand IN ('{item['as_brand']}')" in (response.get("sql") or ""))
        verdict = judge(item, response, gold, judge_mode, executed_ok) if status != 403 else \
            Judgement(correct=True, score=5, reason="403: refused another brand's data")
    passed = executed_ok and verdict.correct
    return {"id": item["id"], "category": item["category"], "question": item["question"], "expect": item["expect"],
            "status": status, "routed_ok": routed_ok, "executed_ok": executed_ok, "judge_correct": verdict.correct,
            "judge_score": verdict.score, "judge_reason": verdict.reason, "passed": passed,
            "latency_s": round(latency, 3), "usage": used.to_dict(), "sql": response.get("sql"),
            "answer": response.get("answer") or response.get("error")}


# --- report --------------------------------------------------------------------------------------
def scorecard(results: list[dict], meta: dict) -> Table:
    table = Table(title=f"Chatbot eval: {meta['n']} questions · model {meta['model']} · judge {meta['judge']} · "
                        f"RAG {meta['rag']}", title_style="bold", header_style="bold cyan",
                  caption=f"{meta['calls']} model calls, {meta['input_tokens']:,} in / {meta['output_tokens']:,} out "
                          f"tokens, ${meta['cost_usd']:.3f} at list prices · {meta['date']}")
    for col, j in (("category", "left"), ("n", "right"), ("routed", "right"), ("result matches", "right"),
                   ("judge pass", "right"), ("judge avg", "right"), ("PASS", "right"), ("p50 latency", "right"),
                   ("cost", "right")):
        table.add_column(col, justify=j)
    order = list(dict.fromkeys(r["category"] for r in results))

    def row(label, rs, bold=False):
        n = len(rs)
        pct = lambda k: f"{100 * sum(r[k] for r in rs) / n:.0f}%"   # noqa: E731
        cells = [label, str(n), pct("routed_ok"), pct("executed_ok"), pct("judge_correct"),
                 f"{statistics.mean(r['judge_score'] for r in rs):.1f}", pct("passed"),
                 f"{statistics.median(r['latency_s'] for r in rs):.1f} s",
                 f"${sum(r['usage']['cost_usd'] for r in rs):.3f}"]
        table.add_row(*(f"[bold]{c}[/bold]" if bold else c for c in cells))

    for cat in order:
        row(cat, [r for r in results if r["category"] == cat])
    table.add_section()
    row("all", results, bold=True)
    return table


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the chatbot")
    parser.add_argument("--only", nargs="*", help="question ids")
    parser.add_argument("--judge", default="model", choices=("model", "fake"))
    parser.add_argument("--max-calls", type=int, default=260, help="stop before more model calls than this")
    parser.add_argument("--max-cost", type=float, default=2.0, help="stop before spending more than this (USD)")
    args = parser.parse_args(argv)

    items = yaml.safe_load(QUESTIONS.read_text())["questions"]
    if args.only:
        items = [q for q in items if q["id"] in set(args.only)]
    store.reset(store.MemoryStore())          # a fresh cache: every question is answered for real
    results, spent = [], usage.Usage()
    console = Console()
    for item in items:
        if spent.llm_calls + 5 > args.max_calls or spent.cost_usd + 0.05 > args.max_cost:
            console.print(f"[yellow]budget guard: stopping before {item['id']} "
                          f"({spent.llm_calls} calls, ${spent.cost_usd:.3f} so far)")
            break
        r = evaluate(item, args.judge)
        for k in ("llm_calls", "input_tokens", "output_tokens", "cost_usd"):
            setattr(spent, k, getattr(spent, k) + r["usage"][k])
        results.append(r)
        console.print(f"{'PASS' if r['passed'] else 'fail'} {r['id']:9} {r['latency_s']:5.1f}s "
                      f"${r['usage']['cost_usd']:.4f}  {r['judge_reason'][:90]}")
    if not results:
        return 1
    meta = {"n": len(results), "model": llm.model_name(), "judge": "fake" if args.judge == "fake" else llm.model_name(),
            "rag": f"{config.RAG_EMBEDDER}" if config.RAG_ENABLED else "off", "calls": spent.llm_calls,
            "input_tokens": spent.input_tokens, "output_tokens": spent.output_tokens,
            "cost_usd": round(spent.cost_usd, 4), "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"eval_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"meta": meta, "results": results}, indent=2, default=str))
    record = Console(record=True, width=150)
    record.print(scorecard(results, meta))
    SCORECARD.parent.mkdir(parents=True, exist_ok=True)
    record.save_svg(str(SCORECARD), title="chatbot eval")
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    console.print(f"\nresults: {shown}\nmodel calls: {spent.llm_calls}, tokens: "
                  f"{spent.input_tokens:,} in / {spent.output_tokens:,} out, estimated cost: ${spent.cost_usd:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
