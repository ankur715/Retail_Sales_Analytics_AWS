"""Retake the milestone-1 README screenshots from the running Codespace stack.

Runs in the Codespace only (headless Chromium is too heavy for the laptop):
  .venv/bin/pip install playwright rich && .venv/bin/python -m playwright install --with-deps chromium
  .venv/bin/python -m scripts.screenshots

Needs `docker compose --profile app --profile obs up` and a finished k6 run
(loadtest/results/{start,mid,end} and k6_{1x,10x}.json). Writes 1440x900 JPEGs to pics/:
  obs_grafana_golden_signals.jpg  the dashboard across the 1x then 10x runs
  obs_jaeger_llm_span.jpg         a typical 10x trace: the LLM spans dominate
  loadtest_k6_10x.jpg             k6 results, 1x vs 10x, rendered with rich
"""
import json
import statistics
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
RESULTS, PICS = ROOT / "loadtest" / "results", ROOT / "pics"
GRAFANA, JAEGER = "http://localhost:3000", "http://localhost:16686"
VIEWPORT = {"width": 1440, "height": 900}


def _epoch(name: str) -> int:
    return int((RESULTS / name).read_text().strip())


def typical_trace(start: int, end: int) -> str:
    """A 10x-run trace near the median duration whose LLM spans take most of the time."""
    url = (f"{JAEGER}/api/traces?service=retail-chatbot&operation=POST%20/api/chat&limit=500"
           f"&start={start * 1_000_000}&end={end * 1_000_000}")
    traces = json.load(urllib.request.urlopen(url))["data"]
    rows = []
    for t in traces:
        root = max(t["spans"], key=lambda s: s["duration"])
        llm = sum(s["duration"] for s in t["spans"] if s["operationName"].startswith("llm."))
        if len(t["spans"]) >= 8:
            rows.append((root["duration"], llm / root["duration"], t["traceID"]))
    median = statistics.median(d for d, _, _ in rows)
    candidates = [r for r in rows if r[1] > 0.9] or rows
    return min(candidates, key=lambda r: abs(r[0] - median))[2]


def k6_svg() -> Path:
    runs = {load: json.loads((RESULTS / f"k6_{load}.json").read_text()) for load in ("1x", "10x")}
    table = Table(title="k6: chatbot load test (fake LLM, ~800 ms per model call, cache bypassed)",
                  title_style="bold", header_style="bold cyan")
    table.add_column("metric")
    for load in runs:
        table.add_column(load, justify="right")

    def m(run, name, stat):
        return run["metrics"][name]["values"][stat]

    rows = [
        ("virtual users", lambda r: f"{m(r, 'vus_max', 'max'):.0f}"),
        ("requests", lambda r: f"{m(r, 'http_reqs', 'count'):.0f}"),
        ("throughput (req/s)", lambda r: f"{m(r, 'http_reqs', 'rate'):.2f}"),
        ("failed", lambda r: f"{100 * m(r, 'http_req_failed', 'rate'):.2f}%"),
        ("checks passed", lambda r: f"{100 * m(r, 'checks', 'rate'):.1f}%"),
        ("latency p50", lambda r: f"{m(r, 'chat_latency', 'med') / 1000:.2f} s"),
        ("latency p95", lambda r: f"{m(r, 'chat_latency', 'p(95)') / 1000:.2f} s"),
        ("latency p99", lambda r: f"{m(r, 'chat_latency', 'p(99)') / 1000:.2f} s"),
        ("latency max", lambda r: f"{m(r, 'chat_latency', 'max') / 1000:.2f} s"),
    ]
    for label, fn in rows:
        table.add_row(label, *(fn(r) for r in runs.values()))
    console = Console(record=True, width=90)
    console.print(table)
    out = RESULTS / "k6_summary.svg"
    console.save_svg(str(out), title="k6 load test")
    return out


def main() -> None:
    start, mid, end = _epoch("start"), _epoch("mid"), _epoch("end")
    svg = k6_svg()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)

        page.goto(f"{GRAFANA}/d/chatbot-golden-signals/?orgId=1&from={(start - 60) * 1000}"
                  f"&to={(end + 60) * 1000}&theme=light&kiosk&refresh=")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(5000)
        page.screenshot(path=str(PICS / "obs_grafana_golden_signals.jpg"), type="jpeg", quality=85)

        page.goto(f"{JAEGER}/trace/{typical_trace(mid, end)}")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(PICS / "obs_jaeger_llm_span.jpg"), type="jpeg", quality=85)

        page.set_viewport_size({"width": 1000, "height": 600})
        page.goto(svg.as_uri())
        page.wait_for_timeout(500)
        page.locator("svg").screenshot(path=str(PICS / "loadtest_k6_10x.jpg"), type="jpeg", quality=85)
        browser.close()
    for name in ("obs_grafana_golden_signals", "obs_jaeger_llm_span", "loadtest_k6_10x"):
        print(name, f"{(PICS / f'{name}.jpg').stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
