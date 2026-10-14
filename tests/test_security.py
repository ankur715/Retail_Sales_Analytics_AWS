"""Milestone 4: login (Keycloak tokens) and per-brand access. Tokens are real RS256 JWTs
signed with a throwaway key; only the key lookup (Keycloak's JWKS) is faked."""
import time
from types import SimpleNamespace

import jwt
import pytest
import sqlglot
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlglot import exp
from test_services import pipeline, redis_store  # noqa: F401  (fixtures)

from chatbot import access, auth, guard, llm
from chatbot import app as chat_app

ISSUER = "http://localhost:8081/realms/retail"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
AURORA, COBALT = frozenset({"Aurora Apparel"}), frozenset({"Cobalt Outdoor"})
VIEWS = ["chat.v_sales_by_week", "chat.v_calendar"]


def token(groups=("brand-C101",), sub="u-aurora", key=KEY, alg="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": ["chatbot-api", "account"], "sub": sub, "iat": now, "exp": now + 300,
              "preferred_username": sub, "groups": list(groups), **overrides}
    return jwt.encode(claims, key, algorithm=alg)


@pytest.fixture
def oidc(monkeypatch):
    monkeypatch.setattr(auth.config, "AUTH_MODE", "oidc")
    monkeypatch.setattr(auth.config, "OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(auth.config, "OIDC_AUDIENCE", "chatbot-api")
    fake = SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=KEY.public_key()))
    monkeypatch.setattr(auth, "jwks_client", lambda: fake)


def ask(client, message="Total sales by retailer", bearer=None, key=None):
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    if key:
        headers["Idempotency-Key"] = key
    return client.post("/v1/chat", json={"message": message}, headers=headers)


# ---------------------------------------------------------------------------
# the SQL guard confines every brand view
# ---------------------------------------------------------------------------
def confined_tables(sql: str) -> dict[str, list[str]]:
    """{view: brands it was confined to} for each brand view in the rewritten SQL."""
    out = {}
    for sub in sqlglot.parse_one(sql, read="redshift").find_all(exp.Subquery):
        inner = sub.this
        source = inner.args.get("from") or inner.args.get("from_") if isinstance(inner, exp.Select) else None
        if source is not None and isinstance(source.this, exp.Table) and inner.args.get("where"):
            t = source.this
            where = inner.args["where"].this
            out[f"{t.db}.{t.name}"] = [e.name for e in where.expressions]
    return out


@pytest.mark.parametrize("sql", [
    "SELECT brand, SUM(sales_usd) FROM chat.v_sales_by_week GROUP BY brand",            # ignores the brand
    "SELECT s.brand FROM chat.v_sales_by_week s JOIN chat.v_calendar c ON c.week_ending = s.week_ending",
    "WITH t AS (SELECT * FROM chat.v_sales_by_week) SELECT * FROM t",
    "SELECT * FROM (SELECT brand FROM chat.v_sales_by_week) x",
    "SELECT brand FROM chat.v_sales_by_week UNION ALL SELECT brand FROM chat.v_sales_by_week",
    "SELECT brand FROM chat.v_sales_by_week WHERE brand = 'Aurora Apparel' OR 1 = 1",   # injection-shaped
])
def test_every_read_of_a_brand_view_is_confined(sql):
    out = guard.validate(sql, VIEWS, AURORA)
    assert confined_tables(out) == {"chat.v_sales_by_week": ["Aurora Apparel"]}
    tree = sqlglot.parse_one(out, read="redshift")
    reads = [t for t in tree.find_all(exp.Table) if t.name == "v_sales_by_week"]
    assert reads and all(t.find_ancestor(exp.Subquery) is not None for t in reads)   # no unconfined read left


def test_views_without_a_brand_column_are_untouched():
    out = guard.validate("SELECT week_ending FROM chat.v_calendar", ["chat.v_calendar"], AURORA)
    assert "IN (" not in out


@pytest.mark.parametrize("literal", ["'Cobalt Outdoor'", "'cobalt outdoor'", "' Cobalt Outdoor '"])
def test_naming_another_brand_is_forbidden(literal):
    with pytest.raises(access.Forbidden, match="Cobalt Outdoor"):
        guard.validate(f"SELECT * FROM chat.v_sales_by_week WHERE brand = {literal}", VIEWS, AURORA)


def test_own_brand_and_unrestricted_callers_pass():
    guard.validate("SELECT * FROM chat.v_sales_by_week WHERE brand = 'Aurora Apparel'", VIEWS, AURORA)
    out = guard.validate("SELECT * FROM chat.v_sales_by_week WHERE brand = 'Cobalt Outdoor'", VIEWS, None)
    assert "IN (" not in out                                  # all-brands / auth off: as before


def test_no_brands_means_no_rows():
    assert "IN (NULL)" in guard.validate("SELECT brand FROM chat.v_sales_by_week", VIEWS, frozenset())


def test_groups_map_to_brands():
    assert access.brands_for_groups(["brand-C101", "/brand-C103"]) == {"Aurora Apparel", "Cobalt Outdoor"}
    assert access.brands_for_groups(["all-brands"]) is None
    assert access.brands_for_groups(["brand-C999", "offline_access"]) == frozenset()


def test_the_model_only_sees_the_callers_brands():
    known = {"chat.v_sales_by_week.brand": ["Aurora Apparel", "Bramble Footwear", "Cobalt Outdoor"],
             "chat.v_sales_by_week.retailer": ["Harbor Mart"]}
    who = access.Principal("u", brands=AURORA)
    assert access.filter_known_values(known, who) == {"chat.v_sales_by_week.brand": ["Aurora Apparel"],
                                                      "chat.v_sales_by_week.retailer": ["Harbor Mart"]}


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------
def test_auth_off_is_anonymous_with_every_brand(pipeline):   # noqa: F811
    assert ask(TestClient(chat_app.app)).status_code == 200


def test_auth_config_tells_the_page_how_to_log_in(oidc):
    body = TestClient(chat_app.app).get("/v1/auth-config").json()
    assert body == {"auth": "oidc", "issuer": ISSUER, "client_id": "chatbot-web"}


@pytest.mark.parametrize("make,detail", [
    (lambda: None, "Sign in"),
    (lambda: "not-a-jwt", "Invalid token"),
    (lambda: token(key=OTHER_KEY), "Invalid token"),                         # forged signature
    (lambda: token(exp=int(time.time()) - 120), "expired"),
    (lambda: token(aud="some-other-api"), "Invalid token"),
    (lambda: token(iss="http://evil.example/realms/retail"), "Invalid token"),
    (lambda: jwt.encode({"sub": "x", "iss": ISSUER, "aud": "chatbot-api", "groups": ["all-brands"],
                         "iat": int(time.time()), "exp": int(time.time()) + 60}, "secret" * 6,
                        algorithm="HS256"), "Invalid token"),               # algorithm swap
])
def test_bad_tokens_are_401(pipeline, oidc, make, detail):   # noqa: F811
    r = ask(TestClient(chat_app.app), bearer=make())
    assert r.status_code == 401 and detail in r.json()["detail"]
    assert r.headers["WWW-Authenticate"].startswith("Bearer")


def test_legacy_route_needs_login_too(pipeline, oidc):   # noqa: F811
    assert TestClient(chat_app.app).post("/api/chat", json={"message": "Total sales"}).status_code == 401


def test_account_without_a_brand_is_403(pipeline, oidc):   # noqa: F811
    assert ask(TestClient(chat_app.app), bearer=token(groups=["offline_access"])).status_code == 403


def test_signed_in_brand_user_gets_only_their_rows(pipeline, oidc, monkeypatch):   # noqa: F811
    seen = []
    monkeypatch.setattr(chat_app.db, "query",
                        lambda sql: seen.append(sql) or {"columns": ["brand"], "rows": [["Aurora Apparel"]],
                                                         "row_count": 1})
    r = ask(TestClient(chat_app.app), bearer=token())
    assert r.status_code == 200 and confined_tables(seen[0]) == {"chat.v_sales_by_week": ["Aurora Apparel"]}


def test_asking_for_another_brand_is_403_without_a_repair_round(pipeline, oidc, monkeypatch):   # noqa: F811
    calls = []

    def write_sql(*args, **kwargs):
        calls.append(1)
        return llm.SqlPlan(sql="SELECT SUM(sales_usd) FROM chat.v_sales_by_week WHERE brand = 'Cobalt Outdoor'",
                           explanation="cobalt")

    monkeypatch.setattr(llm, "write_sql", write_sql)
    r = ask(TestClient(chat_app.app), "How did Cobalt Outdoor do last week?", bearer=token())
    assert r.status_code == 403 and r.json()["detail"] == "You don't have access to Cobalt Outdoor's data"
    assert len(calls) == 1 and pipeline["query"] == 0              # nothing reached the database


def test_all_brands_group_is_unrestricted(pipeline, oidc, monkeypatch):   # noqa: F811
    seen = []
    monkeypatch.setattr(chat_app.db, "query",
                        lambda sql: seen.append(sql) or {"columns": ["b"], "rows": [], "row_count": 0})
    ask(TestClient(chat_app.app), bearer=token(groups=["all-brands"], sub="u-admin"))
    assert confined_tables(seen[0]) == {}


# ---------------------------------------------------------------------------
# isolation between callers
# ---------------------------------------------------------------------------
def test_one_brands_cached_answer_is_never_served_to_another(pipeline, oidc):   # noqa: F811
    client = TestClient(chat_app.app)
    assert not ask(client, bearer=token()).json()["cached"]
    assert ask(client, bearer=token()).json()["cached"]                         # same user: cached
    cobalt = ask(client, bearer=token(groups=["brand-C103"], sub="u-cobalt"))
    assert cobalt.status_code == 200 and not cobalt.json()["cached"]            # other brand: computed afresh


def test_idempotency_keys_are_per_user(pipeline, oidc, monkeypatch):   # noqa: F811
    from chatbot import cache
    monkeypatch.setattr(cache, "get", lambda *a: None)
    client = TestClient(chat_app.app)
    ask(client, bearer=token(), key="shared-key")
    other = ask(client, bearer=token(groups=["brand-C103"], sub="u-cobalt"), key="shared-key")
    assert other.status_code == 200 and "Idempotent-Replayed" not in other.headers
    assert pipeline["write_sql"] == 2


# ---------------------------------------------------------------------------
# the page and its headers
# ---------------------------------------------------------------------------
def test_me_reports_the_verified_brands(oidc):
    r = TestClient(chat_app.app).get("/v1/me", headers={"Authorization": f"Bearer {token()}"})
    assert r.json() == {"username": "u-aurora", "brands": ["Aurora Apparel"]}
    assert TestClient(chat_app.app).get("/v1/me").status_code == 401


def test_page_is_served_with_a_strict_csp(oidc):
    client = TestClient(chat_app.app)
    page = client.get("/")
    csp = page.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert "connect-src 'self' http://localhost:8081" in csp and page.headers["X-Frame-Options"] == "DENY"
    assert "<script>" not in page.text and client.get("/static/app.js").status_code == 200



# ---------------------------------------------------------------------------
# secrets: Vault first, then the environment
# ---------------------------------------------------------------------------
@pytest.fixture
def vault(monkeypatch, tmp_path):
    from etl import secrets
    token_file = tmp_path / "token"
    token_file.write_text("s.chatbot-read-only\n")
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("VAULT_SECRET_PATH", "secret/retail/chatbot")
    secrets._vault_values.cache_clear()
    yield secrets
    secrets._vault_values.cache_clear()


def test_vault_value_wins_and_the_token_comes_from_the_file(vault, monkeypatch):
    calls = []

    def get(url, headers, timeout):
        calls.append((url, headers))
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"data": {"data": {"CHAT_REDSHIFT_PASSWORD": "from-vault"}}})

    monkeypatch.setattr(vault.requests, "get", get)
    monkeypatch.setenv("CHAT_REDSHIFT_PASSWORD", "from-env")
    assert vault.get("CHAT_REDSHIFT_PASSWORD") == "from-vault"
    assert vault.get("NOT_SET_ANYWHERE", "dflt") == "dflt"                   # not in Vault or the env
    assert calls == [("http://vault:8200/v1/secret/data/retail/chatbot",
                      {"X-Vault-Token": "s.chatbot-read-only"})]               # read once, KV v2 path


def test_unreachable_vault_falls_back_to_the_environment(vault, monkeypatch, caplog):
    def down(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr(vault.requests, "get", down)
    monkeypatch.setenv("CHAT_REDSHIFT_PASSWORD", "from-env")
    assert vault.get("CHAT_REDSHIFT_PASSWORD") == "from-env"
    assert "s.chatbot-read-only" not in caplog.text                          # the token is never logged


def test_without_vault_it_is_the_environment(monkeypatch):
    from etl import secrets
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    secrets._vault_values.cache_clear()
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    assert secrets.get("SMTP_PASSWORD") == "app-password"
    secrets._vault_values.cache_clear()
