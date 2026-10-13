"""Chaos tests against the kind cluster (milestone 3). Runs in the Codespace only:

    bash deploy/kind/up.sh && .venv/bin/python -m chaos.run

Steady traffic runs inside the cluster (chaos/traffic.py, through the chatbot Service), while
each experiment breaks one thing and waits for Kubernetes to repair it:

  postgres killed      the database pod is deleted (it comes back re-seeded)
  one sqlgen killed    one of two SQL-generation pods is deleted
  all sqlgen killed    both are deleted at once
  new load (event)     the data changes and a load_completed event goes out on Redpanda
  LLM errors 50%       the fake model fails half its calls, and new data lands meanwhile
  redis killed         the cache is wiped mid-traffic

For each: what users got (fresh / cached / stale fallback / 503 / anything worse), and how
long until the broken part was back. The bar: no 500s, no dropped connections, and only
200s once the dependency is back. Writes loadtest/results/chaos.{json,svg} and
chaos_pods.svg (`kubectl get pods` mid-outage) plus chaos_start/chaos_end for Grafana.
Exits non-zero if any experiment fails the bar.
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "loadtest" / "results"
NS = "retail"
TRAFFIC_POD = "chaos-traffic"
SETTLE_S = 10        # after recovery: this long must be all 200s
records: list[dict] = []
snapshots: dict[str, str] = {}


def kubectl(*args: str, check: bool = True) -> str:
    out = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True)
    if check and out.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def pods(app: str) -> dict[str, bool]:
    """{pod name: ready} for the app's pods that aren't terminating."""
    data = json.loads(kubectl("get", "pods", "-l", f"app={app}", "-o", "json"))
    return {p["metadata"]["name"]: any(c["type"] == "Ready" and c["status"] == "True"
                                       for c in p["status"].get("conditions", []))
            for p in data["items"] if not p["metadata"].get("deletionTimestamp")}


def wait_ready(app: str, count: int, exclude: set[str], timeout: int = 180) -> float:
    """Seconds until `count` new pods (not in `exclude`) are Ready."""
    started = time.time()
    while time.time() - started < timeout:
        ready = [n for n, ok in pods(app).items() if ok and n not in exclude]
        if len(ready) >= count:
            return time.time() - started
        time.sleep(0.5)
    raise TimeoutError(f"{app} not ready after {timeout}s")


def snapshot(name: str, delay: float = 2.0) -> None:
    def take():
        time.sleep(delay)
        snapshots[name] = kubectl("get", "pods", check=False)
    threading.Thread(target=take, daemon=True).start()


def kill(app: str, which: str = "all") -> tuple[float, int, set[str]]:
    names = sorted(pods(app))
    victims = names if which == "all" else names[:1]
    kubectl("delete", "pod", *victims, "--wait=false")
    return time.time(), len(names), set(victims)


def set_error_rate(rate: str) -> None:
    kubectl("set", "env", "deploy/chatbot", "deploy/sqlgen", f"FAKE_LLM_ERROR_RATE={rate}")
    kubectl("rollout", "status", "deploy/chatbot", "deploy/sqlgen", "--timeout=180s")


def new_load(tag: str) -> None:
    """Simulate a load landing: move the data version forward, then announce it on Redpanda
    with the real publisher (etl/events.py), from inside a chatbot pod."""
    kubectl("exec", "deploy/postgres", "--", "psql", "-q", "-U", "postgres", "-d", "retail_dev", "-c",
            "UPDATE chat.v_data_freshness SET last_successful_load_at = now() AT TIME ZONE 'UTC'")
    kubectl("exec", "deploy/chatbot", "--", "python", "-m", "etl.events", "--load-id", f"chaos-{tag}")


# --- experiments: each returns (start, recovered_at) -------------------------------------------
def postgres_killed():
    start, n, old = kill("postgres")
    snapshot("postgres killed")
    return start, start + wait_ready("postgres", n, old)


def one_sqlgen_killed():
    start, n, old = kill("sqlgen", "one")
    snapshot("one sqlgen killed", 1)
    return start, start + wait_ready("sqlgen", n, set())


def all_sqlgen_killed():
    start, n, old = kill("sqlgen")
    snapshot("all sqlgen killed")
    return start, start + wait_ready("sqlgen", n, old)


def load_event():
    start = time.time()
    new_load("event")
    return start, time.time()


def llm_errors():
    start = time.time()
    set_error_rate("0.5")              # rolls both deployments
    snapshot("LLM errors 50%", 0)
    new_load("llm")                    # new data: cached answers stop matching while the model is failing
    time.sleep(30)
    set_error_rate("0")                # back to the manifest's value: Argo CD returns to Synced
    return start, time.time()


def redis_killed():
    start, n, old = kill("redis")
    snapshot("redis killed")
    return start, start + wait_ready("redis", n, old)


EXPERIMENTS = [("postgres killed", postgres_killed), ("one sqlgen killed", one_sqlgen_killed),
               ("all sqlgen killed", all_sqlgen_killed), ("new load (event)", load_event),
               ("LLM errors 50%", llm_errors), ("redis killed", redis_killed)]


# --- traffic ---------------------------------------------------------------------------------
def start_traffic() -> subprocess.Popen:
    kubectl("delete", "pod", TRAFFIC_POD, "--ignore-not-found", "--wait=true")
    kubectl("run", TRAFFIC_POD, "--image=retail-chatbot:local", "--image-pull-policy=Never", "--restart=Never",
            "--command", "--", "python", "-u", "-c", (ROOT / "chaos" / "traffic.py").read_text())
    kubectl("wait", "--for=condition=Ready", f"pod/{TRAFFIC_POD}", "--timeout=120s")
    proc = subprocess.Popen(["kubectl", "-n", NS, "logs", "-f", TRAFFIC_POD], stdout=subprocess.PIPE, text=True)

    def read():
        for line in proc.stdout:
            try:
                records.append(json.loads(line))
            except ValueError:
                pass
    threading.Thread(target=read, daemon=True).start()
    return proc


def outcome(r: dict) -> str:
    if r["s"] == 200:
        return "stale" if r.get("st") else "cached" if r.get("c") else "fresh"
    return "503" if r["s"] == 503 else "worse"   # 500s, other 5xx, dropped connections, timeouts


def summarize(name: str, start: float, recovered: float, end: float) -> dict:
    window = [r for r in records if start <= r["t"] < end]
    counts = {k: sum(outcome(r) == k for r in window) for k in ("fresh", "cached", "stale", "503", "worse")}
    after = [r for r in records if recovered + 2 <= r["t"] < end]          # once it's back (2 s to settle)
    first_fresh = next((r["t"] for r in window if r["t"] >= start and outcome(r) == "fresh"
                        and r["k"] == "pool"), None)
    ok = counts["worse"] == 0 and all(r["s"] == 200 for r in after)
    return {"name": name, **counts, "requests": len(window), "recovery_s": round(recovered - start, 1),
            "first_fresh_pool_s": round(first_fresh - start, 2) if first_fresh else None,
            "after_ok": f"{sum(r['s'] == 200 for r in after)}/{len(after)}", "pass": ok}


def render(rows: list[dict]) -> None:
    table = Table(title="Chaos tests: chatbot on kind (2 chatbot + 2 sqlgen pods, fake LLM)", title_style="bold",
                  header_style="bold cyan", caption="steady traffic through the chatbot Service; 'worse' = 500s, "
                                                    "dropped connections or timeouts")
    for col, j in (("experiment", "left"), ("fresh", "right"), ("cached", "right"), ("stale", "right"),
                   ("503", "right"), ("worse", "right"), ("back in", "right"), ("200s after", "right"),
                   ("result", "left")):
        table.add_column(col, justify=j)
    for r in rows:
        table.add_row(r["name"], str(r["fresh"]), str(r["cached"]), str(r["stale"]), str(r["503"]),
                      Text(str(r["worse"]), style="bold red" if r["worse"] else ""), f"{r['recovery_s']} s",
                      r["after_ok"], Text("PASS", style="bold green") if r["pass"] else Text("FAIL", style="bold red"))
    console = Console(record=True, width=118)
    console.print(table)
    console.save_svg(str(RESULTS / "chaos.svg"), title="chaos tests")

    pick = "postgres killed" if "postgres killed" in snapshots else next(iter(snapshots), None)
    if pick:
        pods_console = Console(record=True, width=86)
        pods_console.print(Panel(Group(Text(f"$ kubectl -n retail get pods     # {pick}, 2 s in", style="dim"),
                                       Text(snapshots[pick].rstrip())), title="during the chaos test",
                                 border_style="cyan"))
        pods_console.save_svg(str(RESULTS / "chaos_pods.svg"), title="kubectl get pods")


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    proc = start_traffic()
    print("warming up (traffic fills the cache and the stale fallback) ...")
    time.sleep(40)
    (RESULTS / "chaos_start").write_text(str(int(time.time())))
    rows = []
    try:
        for name, experiment in EXPERIMENTS:
            print(f"-> {name}")
            start, recovered = experiment()
            time.sleep(SETTLE_S + 5)
            rows.append(summarize(name, start, recovered, time.time()))
            print("  ", rows[-1])
            time.sleep(10)                                   # quiet gap between experiments
    finally:
        (RESULTS / "chaos_end").write_text(str(int(time.time())))
        proc.terminate()
        kubectl("delete", "pod", TRAFFIC_POD, "--ignore-not-found", "--wait=false", check=False)
        set_error_rate("0")
    (RESULTS / "chaos.json").write_text(json.dumps({"experiments": rows, "snapshots": snapshots}, indent=2))
    render(rows)
    Console().print(f"{sum(r['pass'] for r in rows)}/{len(rows)} experiments passed")
    return 0 if all(r["pass"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
