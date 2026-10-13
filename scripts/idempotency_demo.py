"""Idempotency-Key against the running stack (milestone 2): what a retrying client sees.

Runs in the Codespace against `docker compose --profile app up` (fake LLM, no cost):
  .venv/bin/python -m scripts.idempotency_demo

Each question carries a timestamp, so it misses the answer cache and the first run does the
real work. Prints a rich table and saves it to loadtest/results/idempotency.svg (screenshotted
by scripts/screenshots.py --milestone 2). Exits non-zero if any step behaves unexpectedly.
"""
import re
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table

BASE = "http://localhost:8000"
OUT = Path(__file__).resolve().parent.parent / "loadtest" / "results" / "idempotency.svg"


def post(message: str, key: str) -> tuple[requests.Response, float]:
    started = time.perf_counter()
    r = requests.post(f"{BASE}/v1/chat", json={"message": message}, headers={"Idempotency-Key": key}, timeout=60)
    return r, (time.perf_counter() - started) * 1000


def model_runs() -> int:
    """Questions that reached the model: the route step's count on /metrics."""
    text = requests.get(f"{BASE}/metrics", timeout=5).text
    m = re.search(r'^chatbot_step_seconds_count\{step="llm\.route"\} ([\d.e+]+)$', text, re.M)
    return int(float(m.group(1))) if m else 0


def main() -> int:
    stamp = time.strftime("%H:%M:%S")
    question = f"Total sales by retailer last week (demo {stamp})"
    key_a, key_b = f"demo-{uuid.uuid4()}", f"demo-{uuid.uuid4()}"
    runs_before = model_runs()
    rows, ok = [], True

    def row(step, r, ms, expect_status, expect_replay):
        nonlocal ok
        replayed = r.headers.get("Idempotent-Replayed", "-")
        good = r.status_code == expect_status and (replayed == "true") == expect_replay
        ok &= good
        rows.append((step, str(r.status_code), replayed, f"{ms:,.0f} ms", "[green]as expected" if good else "[red]NO"))

    first, ms = post(question, key_a)
    row("1. first request, key A", first, ms, 200, False)
    retry, ms = post(question, key_a)
    row("2. retry: key A, same body", retry, ms, 200, True)
    same = retry.json() == first.json()
    ok &= same
    rows.append(("   identical response body?", "", "", "", "[green]yes" if same else "[red]NO"))
    other, ms = post(question.replace("retailer", "brand"), key_a)
    row("3. key A, different body", other, ms, 422, False)

    # 4. a duplicate sent while the original is still running
    results = {}
    slow = threading.Thread(target=lambda: results.setdefault("orig", post(question + " #2", key_b)))
    slow.start()
    time.sleep(0.3)
    dup, ms = post(question + " #2", key_b)
    row("4. key B while B is running", dup, ms, 409, False)
    slow.join()
    row("5. key B original finishes", *results["orig"], 200, False)
    late, ms = post(question + " #2", key_b)
    row("6. retry: key B, after it finished", late, ms, 200, True)

    runs = model_runs() - runs_before
    ok &= runs == 2
    table = Table(title="Idempotency-Key: POST /v1/chat retried", title_style="bold", header_style="bold cyan",
                  caption=f"{len(rows) - 1} requests, {runs} model runs (expected 2) -- fake LLM, ~800 ms per call")
    for col, justify in (("step", "left"), ("status", "right"), ("replayed", "center"), ("time", "right"),
                         ("check", "left")):
        table.add_column(col, justify=justify)
    for r in rows:
        table.add_row(*r)
    console = Console(record=True, width=86)
    console.print(table)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUT), title="idempotency demo")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
