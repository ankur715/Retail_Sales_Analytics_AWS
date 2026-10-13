"""Analytics chatbot: plain-English questions -> SQL on the curated chat
views -> answer. A separate always-on service; it never touches the ETL
tables or Airflow -- the DAGs only keep the data behind the views fresh.

    POST /v1/chat  {"message": "..."}       (optional header: Idempotency-Key)
      0. cache      same question, same data version, within 15 min -> stored answer (Redis)
      1. route      the LLM picks the views the question needs (catalog summaries only)
      2. write_sql  the LLM writes SQL with ONLY those views' columns/values/examples
                    (the sqlgen gRPC service when SQLGEN_ADDR is set, else in-process)
      3. validate   sqlglot: one SELECT, only the routed views, row cap
      4. run        as the read-only chat_reader user (30s timeout)
         -> on a validation or SQL error, one repair round (back to 2 with the error)
      5. summarize  the LLM answers from the returned rows only

    POST /api/chat, GET /api/health: the pre-/v1 routes, kept for existing clients
    (deprecated; same behaviour, plus Deprecation and Link headers pointing at /v1).

Run:  .venv/bin/uvicorn chatbot.app:app --port 8000   ->  http://localhost:8000  (API docs: /docs)
"""
import time
from pathlib import Path

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from chatbot import cache, catalog, db, guard, idempotency, llm, telemetry
from etl import config
from sqlgen import client as sqlgen

app = FastAPI(title="Retail sales analytics chatbot", version="2.0",
              description="Ask weekly retail sales and inventory questions in plain English. "
                          "Use the **/v1** routes; `/api/*` is kept for existing clients.")
telemetry.setup(app)
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
    error: str | None = None


MESSAGES = {
    "busy": "The language model is busy right now -- please try again in a minute.",
    "quota": "The language model's daily quota is used up -- try again tomorrow.",
    "config": "The language model isn't configured correctly on the server.",
    "declined": "The language model declined to answer that question.",
}


@app.exception_handler(llm.LLMUnavailable)
def llm_unavailable(request: Request, exc: llm.LLMUnavailable):
    # A clear, retryable answer instead of a 500; `reason` carries the detail for the operator
    return JSONResponse(status_code=503, content={"detail": MESSAGES.get(exc.kind, MESSAGES["busy"]),
                                                  "reason": str(exc)})


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


IDEMPOTENCY_KEY = Header(None, alias=idempotency.HEADER,
                         description="Any unique string (e.g. a UUID). A retry with the same key and body "
                                     "returns the first response instead of asking again.")


@v1.post("/chat", response_model=ChatResponse, responses={
    409: {"description": "A request with this Idempotency-Key is still running"},
    422: {"description": "Invalid body, or an Idempotency-Key reused for a different body"},
    503: {"description": "The language model is unavailable; retry later"}})
def chat(req: ChatRequest, idempotency_key: str | None = IDEMPOTENCY_KEY):
    if not idempotency_key:
        return answer(req)
    fp = idempotency.fingerprint(req.model_dump())
    claim = idempotency.begin(idempotency_key, fp)
    if isinstance(claim, idempotency.Replay):
        return JSONResponse(claim.body, status_code=claim.status, headers={"Idempotent-Replayed": "true"})
    try:
        result = answer(req)
    except BaseException:
        if claim:
            idempotency.release(idempotency_key)   # failed: let the retry run for real
        raise
    if claim:
        idempotency.finish(idempotency_key, fp, 200, jsonable_encoder(result))
    return result


def _deprecated(response: Response, successor: str) -> None:
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = f'<{successor}>; rel="successor-version"'


@legacy.get("/health")
def health_legacy(response: Response) -> dict:
    _deprecated(response, "/v1/health")
    return _health()


@legacy.post("/chat", response_model=ChatResponse)
def chat_legacy(req: ChatRequest, response: Response, idempotency_key: str | None = IDEMPOTENCY_KEY):
    """Same as POST /v1/chat."""
    _deprecated(response, "/v1/chat")
    result = chat(req, idempotency_key)
    if isinstance(result, Response):   # a replay is sent as-is, so it needs the headers itself
        _deprecated(result, "/v1/chat")
    return result


def answer(req: ChatRequest) -> ChatResponse:
    started, outcome = time.perf_counter(), "miss"
    try:
        result = _answer(req)
        outcome = "hit" if result.cached else "miss"
        return result
    finally:
        telemetry.ANSWER_SECONDS.labels(outcome).observe(time.perf_counter() - started)


def _answer(req: ChatRequest) -> ChatResponse:
    question = req.message.strip()
    version = cache.data_version()   # read once: the answer is filed under the data it came from
    hit = cache.get(question, version)
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
        known = db.known_values(catalog.known_value_columns(route.views))
    details = catalog.detail_text(route.views, known)
    plan, error, result, sql = sqlgen.write_sql(question, route.views, details), None, None, None
    for attempt in range(MAX_REPAIRS + 1):
        if not plan.sql.strip():
            return ChatResponse(question=question, views=route.views,
                                answer=plan.unanswerable_reason or "That can't be answered from the available data.")
        try:
            with telemetry.step("sql.validate"):
                sql = guard.validate(plan.sql, route.views)
            result = db.query(sql)
            error = None
            break
        except Exception as e:   # UnsafeSQL or a database error: give the model the exact message once
            error = str(e).strip().splitlines()[0][:300]
            if attempt < MAX_REPAIRS:
                plan = sqlgen.write_sql(question, route.views, details, previous_error=error, previous_sql=plan.sql)
    if error:
        return ChatResponse(question=question, views=route.views, sql=sql or plan.sql, error=error,
                            answer="Sorry, I couldn't build a working query for that. Try rephrasing.")

    # 5. answer from the rows only
    with telemetry.step("db.data_as_of"):
        as_of = db.data_as_of()
    summary = llm.summarize(question, sql, result["columns"], result["rows"], as_of)
    payload = dict(question=question, answer=summary, views=route.views, sql=sql, explanation=plan.explanation,
                   columns=result["columns"], rows=result["rows"], row_count=result["row_count"], data_as_of=as_of)
    cache.put(question, version, payload)
    return ChatResponse(**payload)


app.include_router(v1)
app.include_router(legacy)
