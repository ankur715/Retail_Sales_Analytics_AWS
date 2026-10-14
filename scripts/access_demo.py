"""Per-brand access against the running stack (milestone 4): who can see what.

Runs in the Codespace after `bash deploy/auth/up.sh` (fake LLM, no cost):
  .venv/bin/python -m scripts.access_demo

Signs in as the realm's test users through Keycloak (the `chatbot-test` client's password
grant; the password is read from .secrets/local.env, never printed) and asks the chatbot:
  - no token                                  -> 401
  - a forged token                            -> 401
  - Aurora's analyst, about Aurora            -> 200, rows only for Aurora Apparel
  - Aurora's analyst, about Cobalt            -> 403
  - Aurora's analyst, "all brands"            -> 200, still only Aurora's rows
  - Cobalt's analyst, about Cobalt            -> 200
  - the all-brands admin, all brands          -> 200, every brand
The fake LLM writes SQL from catalog examples, so to make the model name a specific brand
each question includes it; the SQL guard -- not the model -- decides what comes back.
Saves a rich table to loadtest/results/access.svg (scripts/screenshots.py --milestone 4).
Exits non-zero if any row is unexpected.
"""
import base64
import json
import sys
import time
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
CHATBOT, KEYCLOAK = "http://localhost:8000", "http://localhost:8081"
OUT = ROOT / "loadtest" / "results" / "access.svg"


def password() -> str:
    env = dict(line.split("=", 1) for line in (ROOT / ".secrets" / "local.env").read_text().split() if "=" in line)
    return env["KC_TEST_USER_PASSWORD"]


def login(user: str) -> str:
    r = requests.post(f"{KEYCLOAK}/realms/retail/protocol/openid-connect/token", timeout=10,
                      data={"grant_type": "password", "client_id": "chatbot-test", "username": user,
                            "password": password(), "scope": "openid"})
    r.raise_for_status()
    return r.json()["access_token"]


def forged(real: str) -> str:
    """A real token with its groups rewritten to all-brands: the signature no longer matches."""
    head, body, sig = real.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    claims["groups"] = ["all-brands"]
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{head}.{body}.{sig}"


def ask(question: str, token: str | None) -> tuple[int, str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.post(f"{CHATBOT}/v1/chat", json={"message": f"{question} ({time.time_ns()})"}, headers=headers,
                      timeout=60)
    body = r.json()
    if r.status_code != 200:
        return r.status_code, body.get("detail", "")
    brands = sorted({row[body["columns"].index("brand")] for row in body["rows"]}) if "brand" in body["columns"] \
        else []
    return 200, f"{body['row_count']} rows; brands: {', '.join(brands) or '-'}"


def main() -> int:
    aurora, cobalt, admin = login("aurora.analyst"), login("cobalt.analyst"), login("retail.admin")
    cases = [
        ("no token", None, "Sales by brand for Aurora Apparel in the last 4 weeks", 401, None),
        ("forged token (groups edited)", forged(aurora), "Sales by brand for every brand", 401, None),
        ("aurora.analyst", aurora, "Sales by brand for Aurora Apparel in the last 4 weeks", 200, ["Aurora Apparel"]),
        ("aurora.analyst", aurora, "Sales by brand for Cobalt Outdoor in the last 4 weeks", 403, None),
        ("aurora.analyst", aurora, "Sales by brand for every brand in the last 4 weeks", 200, ["Aurora Apparel"]),
        ("cobalt.analyst", cobalt, "Sales by brand for Cobalt Outdoor in the last 4 weeks", 200, ["Cobalt Outdoor"]),
        ("retail.admin (all-brands)", admin, "Sales by brand for every brand in the last 4 weeks", 200,
         ["Aurora Apparel", "Bramble Footwear", "Cobalt Outdoor"]),
    ]
    table = Table(title="Per-brand access: POST /v1/chat with Keycloak tokens", title_style="bold",
                  header_style="bold cyan", caption="the SQL guard confines every query to the caller's brands; "
                                                    "fake LLM, local Keycloak realm")
    for col in ("caller", "question", "status", "what came back", "check"):
        table.add_column(col, justify="right" if col == "status" else "left")
    ok = True
    for who, token, question, want_status, want_brands in cases:
        status, detail = ask(question, token)
        good = status == want_status and (want_brands is None or detail.endswith(", ".join(want_brands)))
        ok &= good
        table.add_row(who, question, Text(str(status), style="green" if status == 200 else "yellow"), detail,
                      Text("as expected", style="green") if good else Text("NO", style="bold red"))
    console = Console(record=True, width=150)
    console.print(table)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUT), title="per-brand access")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
