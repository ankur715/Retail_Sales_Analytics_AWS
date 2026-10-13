"""Steady chat traffic from INSIDE the cluster, through the chatbot Service -- what real users
hit, load-balanced across replicas. chaos/run.py starts it as a pod and reads its stdout:
one JSON line per response. Stdlib + requests only (it runs in the chatbot image)."""
import json
import os
import random
import threading
import time
import uuid

import requests

BASE = os.environ.get("BASE_URL", "http://chatbot:8000")
WORKERS = int(os.environ.get("WORKERS", "4"))
POOL = [f"{q} (#{i})" for q in ("Total sales by retailer last week", "Top 5 styles by sales at Harbor Mart",
                                "Week over week sales change by brand", "Which products were out of stock last week?")
        for i in range(5)]   # 20 questions asked over and over: cached, and the stale fallback exists for them
lock = threading.Lock()


def worker():
    session = requests.Session()
    while True:
        pool = random.random() < 0.7
        q = random.choice(POOL) if pool else f"Total sales by brand ({uuid.uuid4().hex[:8]})"
        started = time.time()
        line = {"t": started, "k": "pool" if pool else "unique"}
        try:
            r = session.post(f"{BASE}/v1/chat", json={"message": q}, timeout=30)
            line["s"] = r.status_code
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            line.update(c=bool(body.get("cached")), st=bool(body.get("stale")))
            if r.status_code >= 500:
                line["d"] = str(body.get("detail", r.text))[:60]
        except Exception as e:   # connection refused / reset / timeout: the worst outcome
            line.update(s=0, d=type(e).__name__)
            session = requests.Session()
            time.sleep(0.2)
        line["ms"] = round((time.time() - started) * 1000)
        with lock:
            print(json.dumps(line), flush=True)


for _ in range(WORKERS):
    threading.Thread(target=worker, daemon=True).start()
while True:
    time.sleep(3600)
