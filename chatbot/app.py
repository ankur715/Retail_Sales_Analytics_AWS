"""Analytics chatbot: plain-English questions -> SQL on the curated chat
views -> answer. A separate always-on service; it never touches the ETL
tables or Airflow -- the DAGs only keep the data behind the views fresh.

    POST /v1/chat  {"message": "..."}       (optional header: Idempotency-Key)
      auth          AUTH_MODE=oidc: a Keycloak token decides who asks and which brands they
                    see; every query is confined to those brands (chatbot/auth.py, access.py)
      0. cache      same question, same data version, within 15 min -> stored answer (Redis)
      1. route      the LLM picks the views the question needs (catalog summaries only)
      2. write_sql  the LLM writes SQL with ONLY those views' columns/values/examples
                    (the sqlgen gRPC service when SQLGEN_ADDR is set, else in-process)
      3. validate   sqlglot: one SELECT, only the routed views, row cap
      4. run        as the read-only chat_reader user (30s timeout)
         -> on a validation or SQL error, one repair round (back to 2 with the error)
      5. summarize  the LLM answers from the returned rows only
      fallback      model or database down -> the last answer to this question (stale: true), else 503

    POST /api/chat, GET /api/health: the pre-/v1 routes, kept for existing clients
    (deprecated; same behaviour, plus Deprecation and Link headers pointing at /v1).

Run:  .venv/bin/uvicorn chatbot.app:app --port 8000   ->  http://localhost:8000  (API docs: /docs)
"""
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from chatbot import access, auth, cache, catalog, db, events, guard, idempotency, llm, telemetry, usage
from etl import config
from rag.retrieve import retrieve as rag_retrieve
from sqlgen import client as sqlgen

@asynccontextmanager
async def lifespan(app: FastAPI):
    events.start()   # load_completed listener, only when EVENTS_BOOTSTRAP is set
    yield
    events.stop()


app = FastAPI(title="Retail sales analytics chatbot", version="2.0", lifespan=lifespan,
              description="Ask weekly retail sales and inventory questions in plain English. "
                          "Use the **/v1** routes; `/api/*` is kept for existing clients.")
telemetry.setup(app)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Browser hardening for the chat page and API. The CSP allows scripts only from this
    origin (no inline script), and network calls only to this origin and Keycloak."""
    response = await call_next(request)
    if request.url.path in ("/docs", "/redoc"):   # Swagger UI loads its assets from a CDN
        return response
    login_origin = "/".join(config.OIDC_ISSUER.split("/")[:3]) if config.AUTH_MODE == "oidc" else ""
    response.headers.update({
        "Content-Security-Policy": f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                   f"connect-src 'self' {login_origin}".strip() + "; frame-ancestors 'none'; "
                                   "base-uri 'none'; form-action 'self'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
    })
    return response
v1 = APIRouter(prefix="/v1", tags=["v1"])
legacy = APIRouter(prefix="/api", tags=["legacy (deprecated)"], deprecated=True)
STATIC = Path(__file__).resolve().parent / "static"
MAX_REPAIRS = 1


class ChatRequest(BaseModel):
    message: str = Field(min_length=3, max_length=500, examples=["Total sales by retailer last week"])


class ChatResponse(BaseModel):
    question: str
    answer: str
    views: list[str] = []
    sql: str | None = None
    explanation: str | None = None
    columns: list[str] = []
    rows: list[list] = []
    row_count: int = 0
    data_as_of: str | None = None
    cached: bool = False
    stale: bool = False     # the live path failed; this is the last answer we had (see data_as_of)
    error: str | None = None
    usage: dict | None = None   # LLM calls, tokens and cost of THIS response (zero for a cache hit)


MESSAGES = {
    "busy": "The language model is busy right now -- please try again in a minute.",
    "quota": "The language model's daily quota is used up -- try again tomorrow.",
    "config": "The language model isn't configured correctly on the server.",
    "declined": "The language model declined to answer that question.",
    "database": "The database is unavailable right now -- please try again shortly.",
}


@app.exception_handler(llm.LLMUnavailable)
def llm_unavailable(request: Request, exc: llm.LLMUnavailable):
    # A clear, retryable answer instead of a 500; `reason` carries the detail for the operator
    return JSONResponse(status_code=503, content={"detail": MESSAGES.get(exc.kind, MESSAGES["busy"]),
                                                  "reason": str(exc)})


@app.exception_handler(access.Forbidden)
def forbidden(request: Request, exc: access.Forbidden):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(db.DatabaseUnavailable)
def database_unavailable(request: Request, exc: db.DatabaseUnavailable):
    return JSONResponse(status_code=503, content={"detail": MESSAGES["database"], "reason": str(exc)})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


def _health() -> dict:
    return {"status": "ok", "env": config.ENV, "database": config.REDSHIFT_DB, "provider": config.LLM_PROVIDER,
            "model": llm.model_name(), "llm_configured": llm.configured(), "views": catalog.view_names(),
            "cache": "redis" if config.REDIS_URL else "in-process", "sqlgen": config.SQLGEN_ADDR or "in-process"}


@v1.get("/health")
def health() -> dict:
    return _health()


@v1.get("/auth-config", include_in_schema=False)
def auth_config() -> dict:
    return auth.page_config()


@v1.get("/me")
def me(who: access.Principal = Depends(auth.principal)) -> dict:
    """Who the server thinks you are, and the brands you can see (null = all)."""
    return {"username": who.username, "brands": sorted(who.brands) if who.brands is not None else None}


IDEMPOTENCY_KEY = Header(None, alias=idempotency.HEADER,
                         description="Any unique string (e.g. a UUID). A retry with the same key and body "
                                     "returns the first response instead of asking again.")


@v1.post("/chat", response_model=ChatResponse, responses={
    401: {"description": "Not signed in, or the token is invalid or expired (AUTH_MODE=oidc)"},
    403: {"description": "The question needs another brand's data, or the account has no brand"},
    409: {"description": "A request with this Idempotency-Key is still running"},
    422: {"description": "Invalid body, or an Idempotency-Key reused for a different body"},
    503: {"description": "The language model is unavailable; retry later"}})
def chat(req: ChatRequest, idempotency_key: str | None = IDEMPOTENCY_KEY,
         who: access.Principal = Depends(auth.principal)):
    if not idempotency_key:
        return answer(req, who)
    fp = idempotency.fingerprint(req.model_dump())
    claim = idempotency.begin(idempotency_key, fp, who.subject)
    if isinstance(claim, idempotency.Replay):
        return JSONResponse(claim.body, status_code=claim.status, headers={"Idempotent-Replayed": "true"})
    try:
        result = answer(req, who)
    except BaseException:
        if claim:
            idempotency.release(idempotency_key, who.subject)   # failed: let the retry run for real
        raise
    if claim:
        if result.stale:   # a fallback isn't the answer to this request: let a retry try the live path
            idempotency.release(idempotency_key, who.subject)
        else:
            idempotency.finish(idempotency_key, fp, 200, jsonable_encoder(result), who.subject)
    return result


def _deprecated(response: Response, successor: str) -> None:
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = f'<{successor}>; rel="successor-version"'


@legacy.get("/health")
def health_legacy(response: Response) -> dict:
    _deprecated(response, "/v1/health")
    return _health()


@legacy.post("/chat", response_model=ChatResponse)
def chat_legacy(req: ChatRequest, response: Response, idempotency_key: str | None = IDEMPOTENCY_KEY,
                who: access.Principal = Depends(auth.principal)):
    """Same as POST /v1/chat."""
    _deprecated(response, "/v1/chat")
    result = chat(req, idempotency_key, who)
    if isinstance(result, Response):   # a replay is sent as-is, so it needs the headers itself
        _deprecated(result, "/v1/chat")
    return result


def answer(req: ChatRequest, who: access.Principal = access.ANONYMOUS) -> ChatResponse:
    with usage.track() as used:
        result = _answer_or_fallback(req, who, time.perf_counter())
    if used.llm_calls and not result.cached:
        usage.REQUEST_COST.observe(used.cost_usd)
    return result.model_copy(update={"usage": used.to_dict()})


def _answer_or_fallback(req: ChatRequest, who: access.Principal, started: float) -> ChatResponse:
    outcome = "miss"
    try:
        try:
            result = _answer(req, who)
        except (llm.LLMUnavailable, db.DatabaseUnavailable):
            # Stale-if-error: the last answer to this question (any data version) beats a 503.
            # The response says stale, and data_as_of says how old the data is.
            last = cache.last(req.message.strip(), who.scope)
            if not last:
                raise
            telemetry.CACHE.labels("stale").inc()
            result = ChatResponse(**{**last, "cached": True, "stale": True})
        outcome = "stale" if result.stale else "hit" if result.cached else "miss"
        return result
    finally:
        telemetry.ANSWER_SECONDS.labels(outcome).observe(time.perf_counter() - started)


def _answer(req: ChatRequest, who: access.Principal) -> ChatResponse:
    question = req.message.strip()
    version = cache.data_version()   # read once: the answer is filed under the data it came from
    hit = cache.get(question, version, who.scope)
    if hit:
        telemetry.CACHE.labels("hit").inc()
        return ChatResponse(**{**hit, "cached": True})
    telemetry.CACHE.labels("miss").inc()
    if not llm.configured():
        raise HTTPException(503, "LLM features are turned off on the server (LLM_PROVIDER=none)"
                            if config.LLM_PROVIDER == "none" else
                            f"The {config.LLM_PROVIDER} LLM is not configured on the server")

    # 1. route: only the views this question needs
    route = llm.route(question)
    if not route.views:
        return ChatResponse(question=question, answer=f"I can only answer questions about weekly retail "
                            f"sales and inventory data. ({route.reason})")

    # 2-4. write SQL with only those views' metadata, validate, run; one repair round on failure
    with telemetry.step("db.known_values"):
        known = access.filter_known_values(db.known_values(catalog.known_value_columns(route.views)), who)
    retrieved = rag_retrieve(question, route.views, who)   # None when RAG is off or unavailable
    details = catalog.detail_text(route.views, known, examples=retrieved.examples if retrieved else None)
    plan, error, result, sql = sqlgen.write_sql(question, route.views, details), None, None, None
    for attempt in range(MAX_REPAIRS + 1):
        if not plan.sql.strip():
            return ChatResponse(question=question, views=route.views,
                                answer=plan.unanswerable_reason or "That can't be answered from the available data.")
        try:
            with telemetry.step("sql.validate"):
                sql = guard.validate(plan.sql, route.views, who.brands)
            result = db.query(sql)
            error = None
            break
        except (db.DatabaseUnavailable, access.Forbidden):
            raise                # not the query's fault: no repair round (503 / fallback, or 403)
        except Exception as e:   # UnsafeSQL or a query error: give the model the exact message once
            error = str(e).strip().splitlines()[0][:300]
            if attempt < MAX_REPAIRS:
                plan = sqlgen.write_sql(question, route.views, details, previous_error=error, previous_sql=plan.sql)
    if error:
        return ChatResponse(question=question, views=route.views, sql=sql or plan.sql, error=error,
                            answer="Sorry, I couldn't build a working query for that. Try rephrasing.")

    # 5. answer from the rows only
    with telemetry.step("db.data_as_of"):
        as_of = db.data_as_of()
    summary = llm.summarize(question, sql, result["columns"], result["rows"], as_of,
                            notes=retrieved.notes if retrieved else None)
    payload = dict(question=question, answer=summary, views=route.views, sql=sql, explanation=plan.explanation,
                   columns=result["columns"], rows=result["rows"], row_count=result["row_count"], data_as_of=as_of)
    cache.put(question, version, payload, who.scope)
    return ChatResponse(**payload)


app.include_router(v1)
app.include_router(legacy)
