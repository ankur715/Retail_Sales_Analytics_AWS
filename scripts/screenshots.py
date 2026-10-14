"""Retake the README screenshots from the running Codespace stack.

Runs in the Codespace only (headless Chromium is too heavy for the laptop):
  .venv/bin/pip install playwright rich && .venv/bin/python -m playwright install --with-deps chromium
  .venv/bin/python -m scripts.screenshots --milestone 1   (or 2)

Needs `docker compose --profile app --profile obs up`. Writes 1440x900-wide JPEGs to pics/.

Milestone 1 -- after the k6 1x then 10x runs (loadtest/results/{start,mid,end}, k6_{1x,10x}.json):
  obs_grafana_golden_signals.jpg  the dashboard across the 1x then 10x runs
  obs_jaeger_llm_span.jpg         a typical 10x trace: the LLM spans dominate
  loadtest_k6_10x.jpg             k6 results, 1x vs 10x, rendered with rich
Milestone 2 -- after a CACHE=on k6 run (loadtest/results/{cache_start,cache_end}) and
scripts.idempotency_demo (loadtest/results/idempotency.svg):
  api_swagger_v1.jpg              /docs: the /v1 routes, Idempotency-Key, legacy /api deprecated
  cache_hit_vs_miss.jpg           Grafana: answer latency on a cache hit vs a miss, hit ratio
  idempotency_replay.jpg          the demo's table: replay, 422, 409, model runs
Milestone 3 -- on the kind cluster (deploy/kind/up.sh, then deploy/kind/forward.sh) after
chaos.run (loadtest/results/chaos*.svg, chaos_start, chaos_end):
  argocd_app_synced.jpg           Argo CD: retail-chatbot Synced + Healthy, its resource tree
  chaos_kubectl_pods.jpg          `kubectl get pods` two seconds into the Postgres kill
  chaos_results.jpg               the chaos table: what users got per experiment
  slo_error_budget.jpg            Grafana SLO dashboard across the chaos run
Milestone 4 -- after deploy/auth/up.sh and scripts.access_demo (loadtest/results/access.svg);
PR=<number> for the CI checks shot:
  auth_keycloak_login.jpg         the chatbot redirects to Keycloak's sign-in page
  auth_forbidden_other_brand.jpg  signed in as aurora.analyst, a Cobalt question gets the 403
  auth_access_matrix.jpg          the access demo's table (401 / 403 / confined rows)
  ci_security_scans.jpg           the PR's checks on GitHub, security scans passing
"""
import argparse
import json
import statistics
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
RESULTS, PICS = ROOT / "loadtest" / "results", ROOT / "pics"
GRAFANA, JAEGER, CHATBOT = "http://localhost:3000", "http://localhost:16686", "http://localhost:8000"
ARGOCD = "http://localhost:8080"
VIEWPORT = {"width": 1440, "height": 900}


def _epoch(name: str) -> int:
    return int((RESULTS / name).read_text().strip())


def typical_trace(start: int, end: int) -> str:
    """A 10x-run trace near the median duration whose LLM spans take most of the time."""
    url = (f"{JAEGER}/api/traces?service=retail-chatbot&operation=POST%20/v1/chat&limit=500"
           f"&start={start * 1_000_000}&end={end * 1_000_000}")
    # bandit B310 (urlopen schemes): the URL is the fixed local Jaeger API built above
    traces = json.load(urllib.request.urlopen(url))["data"]  # nosec B310
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
    table = Table(title="k6 load test: chatbot on the fake LLM", title_style="bold", header_style="bold cyan",
                  caption="~800 ms per model call, answer cache bypassed")
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
    console = Console(record=True, width=48)
    console.print(table)
    out = RESULTS / "k6_summary.svg"
    console.save_svg(str(out), title="k6 load test")
    return out


def svg_shot(page, svg: Path, out: Path) -> None:
    page.set_content("<html><body style='margin:0;background:#fff'><style>svg{width:900px;height:auto;"
                     "display:block}</style>" + svg.read_text() + "</body></html>")
    page.wait_for_timeout(500)
    page.locator("svg").screenshot(path=str(out), type="jpeg", quality=85)


def milestone2() -> list[str]:
    start, end = _epoch("cache_start"), _epoch("cache_end")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)

        # Swagger: every route collapsed except POST /v1/chat, opened to show the Idempotency-Key header
        page.goto(f"{CHATBOT}/docs")
        page.wait_for_selector(".opblock")
        page.locator(".opblock-post", has_text="/v1/chat").first.locator(".opblock-summary").click()
        page.wait_for_selector("text=Idempotency-Key")
        # keep just its parameters, so the deprecated /api group still fits below
        page.add_style_tag(content=".opblock-section-request-body, .responses-wrapper, .opblock-section-header"
                                   ":has(+ .opblock-description-wrapper) {display: none !important}")
        page.wait_for_timeout(500)
        page.screenshot(path=str(PICS / "api_swagger_v1.jpg"), type="jpeg", quality=85,
                        clip={"x": 0, "y": 0, "width": 1440, "height": 900})

        # Grafana: the milestone 2 row (hit vs miss latency, hit ratio, replays) over the cache run.
        # Grafana only renders panels inside the viewport, so this page is tall enough to hold that row.
        page = browser.new_page(viewport={"width": 1440, "height": 1500})
        page.goto(f"{GRAFANA}/d/chatbot-golden-signals/?orgId=1&from={(start - 30) * 1000}"
                  f"&to={(end + 10) * 1000}&theme=light&kiosk&refresh=")
        page.wait_for_load_state("networkidle")
        header = page.get_by_text("Answer latency: cache hit vs miss", exact=False).first
        page.wait_for_timeout(5000)
        top = header.bounding_box()["y"] - 12
        page.screenshot(path=str(PICS / "cache_hit_vs_miss.jpg"), type="jpeg", quality=85,
                        clip={"x": 0, "y": max(top, 0), "width": 1440, "height": 330})

        svg_shot(browser.new_page(viewport=VIEWPORT), RESULTS / "idempotency.svg", PICS / "idempotency_replay.jpg")
        browser.close()
    return ["api_swagger_v1", "cache_hit_vs_miss", "idempotency_replay"]


def milestone3() -> list[str]:
    start, end = _epoch("chaos_start"), _epoch("chaos_end")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)

        # Argo CD (anonymous read-only): the app's sync/health and resource tree
        page.goto(f"{ARGOCD}/applications/argocd/retail-chatbot?view=tree")
        page.wait_for_selector("text=Synced", timeout=60_000)
        page.wait_for_timeout(4000)
        # Redact: the sync panel shows commit authors' email addresses
        page.evaluate(r"""() => {
            const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            for (let n; (n = walk.nextNode());)
                n.nodeValue = n.nodeValue.replace(/\s*<?[\w.+-]+@[\w-]+(\.[\w-]+)+>?/g, "");
        }""")
        # zoom the resource tree out so the Deployments and pods fit, not just the ConfigMaps
        page.add_style_tag(content=".application-resource-tree {zoom: 0.5}")
        page.wait_for_timeout(1000)
        page.screenshot(path=str(PICS / "argocd_app_synced.jpg"), type="jpeg", quality=85)

        # Grafana SLO dashboard over the chaos run (budget spent, burn rate, alerts, fallbacks)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"{GRAFANA}/d/chatbot-slo/?orgId=1&from={(start - 60) * 1000}&to={(end + 60) * 1000}"
                  f"&theme=light&kiosk&refresh=")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(6000)
        page.screenshot(path=str(PICS / "slo_error_budget.jpg"), type="jpeg", quality=85,
                        clip={"x": 0, "y": 0, "width": 1440, "height": 800})

        svg_shot(browser.new_page(viewport=VIEWPORT), RESULTS / "chaos_pods.svg", PICS / "chaos_kubectl_pods.jpg")
        svg_shot(browser.new_page(viewport=VIEWPORT), RESULTS / "chaos.svg", PICS / "chaos_results.jpg")
        browser.close()
    return ["argocd_app_synced", "chaos_kubectl_pods", "chaos_results", "slo_error_budget"]


def milestone4() -> list[str]:
    import os
    env = dict(line.split("=", 1) for line in (ROOT / ".secrets" / "local.env").read_text().split() if "=" in line)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)

        page.goto(CHATBOT)                                    # not signed in -> Keycloak
        page.wait_for_selector("#username", timeout=60_000)
        page.wait_for_timeout(1000)
        page.screenshot(path=str(PICS / "auth_keycloak_login.jpg"), type="jpeg", quality=85)

        page.fill("#username", "aurora.analyst")              # local test user, generated password
        page.fill("#password", env["KC_TEST_USER_PASSWORD"])
        page.click("#kc-login")
        page.wait_for_selector("#who.on", timeout=60_000)     # back on the chat page, signed in
        page.fill("#q", "Sales by brand for Cobalt Outdoor in the last 4 weeks")
        page.click("#send")
        page.wait_for_selector(".msg .err", timeout=60_000)
        page.wait_for_timeout(500)
        page.screenshot(path=str(PICS / "auth_forbidden_other_brand.jpg"), type="jpeg", quality=85,
                        clip={"x": 0, "y": 0, "width": 1440, "height": 520})

        svg_shot(browser.new_page(viewport=VIEWPORT), RESULTS / "access.svg", PICS / "auth_access_matrix.jpg")

        pr = os.environ.get("PR")
        if pr:                                                # public repo: no login needed
            gh = browser.new_page(viewport={"width": 1440, "height": 1100})
            gh.goto(f"https://github.com/ankur715/Retail_Sales_Analytics_AWS/pull/{pr}/checks")
            gh.wait_for_load_state("networkidle")
            gh.wait_for_timeout(3000)
            gh.screenshot(path=str(PICS / "ci_security_scans.jpg"), type="jpeg", quality=85,
                          clip={"x": 0, "y": 0, "width": 1440, "height": 900})
        browser.close()
    return ["auth_keycloak_login", "auth_forbidden_other_brand", "auth_access_matrix"] + \
        (["ci_security_scans"] if os.environ.get("PR") else [])


def milestone1() -> list[str]:
    start, mid, end = _epoch("start"), _epoch("mid"), _epoch("end")
    svg = k6_svg()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)

        # end the range while 10x is still running, so the tiles show the 10x values, not the wind-down
        page.goto(f"{GRAFANA}/d/chatbot-golden-signals/?orgId=1&from={(start - 30) * 1000}"
                  f"&to={(end - 20) * 1000}&theme=light&kiosk&refresh=")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(5000)
        page.screenshot(path=str(PICS / "obs_grafana_golden_signals.jpg"), type="jpeg", quality=85,
                        clip={"x": 0, "y": 60, "width": 1440, "height": 764})   # signals + traffic/latency + steps

        page.goto(f"{JAEGER}/trace/{typical_trace(mid, end)}")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(PICS / "obs_jaeger_llm_span.jpg"), type="jpeg", quality=85,
                        clip={"x": 0, "y": 0, "width": 1440, "height": 620})

        svg_shot(page, svg, PICS / "loadtest_k6_10x.jpg")
        browser.close()
    return ["obs_grafana_golden_signals", "obs_jaeger_llm_span", "loadtest_k6_10x"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--milestone", type=int, choices=(1, 2, 3, 4), default=4)
    names = {1: milestone1, 2: milestone2, 3: milestone3, 4: milestone4}[parser.parse_args().milestone]()
    for name in names:
        print(name, f"{(PICS / f'{name}.jpg').stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
