"""Fake LLM provider for load and chaos tests: canned answers, no model, no cost.

Same interface as the real providers (json_call, text_call, model_name), so the
chatbot runs its full path -- route, write SQL, guard, query, summarize -- with only
the model swapped out. Answers are deterministic, taken from config/chat_catalog.yaml:

  route()      the view whose keywords match the question (product/style/stock words ->
               chat.v_weekly_sales, everything else -> chat.v_sales_by_week)
  write_sql()  "... by brand ..." -> sales per brand (filtered to a brand the question names);
               otherwise the first catalog example that uses only the allowed view
  summarize()  a one-line answer quoting the row count

  FAKE_LLM_LATENCY_MS   mean delay per call (exponentially distributed, like a real
                        model's long tail); 0 = no delay
  FAKE_LLM_ERROR_RATE   share of calls (0..1) raising LLMUnavailable("busy"), the same
                        error a throttled real provider raises
"""
import random
import re
import time

from pydantic import BaseModel

from chatbot import catalog, usage
from chatbot.llm_types import LLMUnavailable, Route, SqlPlan
from etl import config

PRODUCT_WORDS = re.compile(r"\b(products?|styles?|colou?rs?|sizes?|sku|stock|out of stock|stockouts?|top|bottom)\b",
                           re.IGNORECASE)


def model_name() -> str:
    return "fake"


def _behave():
    """Simulated latency and errors, read per call so a test can change them on the fly."""
    if config.FAKE_LLM_LATENCY_MS > 0:
        time.sleep(random.expovariate(1000 / config.FAKE_LLM_LATENCY_MS))
    if random.random() < config.FAKE_LLM_ERROR_RATE:
        raise LLMUnavailable("Fake LLM injected error (FAKE_LLM_ERROR_RATE)", kind="busy")


def _question(prompt: str) -> str:
    found = re.findall(r'Question: "(.*)"', prompt)
    return found[-1] if found else ""


def _route(prompt: str) -> Route:
    view = "chat.v_weekly_sales" if PRODUCT_WORDS.search(_question(prompt)) else "chat.v_sales_by_week"
    if view not in catalog.view_names():
        view = catalog.view_names()[0]
    return Route(views=[view], reason="fake: keyword match")


def _by_brand(question: str) -> str:
    """"... by brand ..." -> sales per brand over the last 4 weeks, filtered to a brand the question names
    (like a real model would). Whether that brand is visible is the SQL guard's call, not ours."""
    named = [b for b in catalog.load()["access"]["client_brands"].values() if b.lower() in question.lower()]
    where = "weeks_ago BETWEEN 0 AND 3" + (f" AND brand = '{named[0]}'" if named else "")
    return (f"SELECT brand, ROUND(SUM(sales_usd), 2) AS sales_usd FROM chat.v_sales_by_week WHERE {where} "
            "GROUP BY brand ORDER BY sales_usd DESC")


def _sql(prompt: str) -> SqlPlan:
    allowed = re.search(r"Use ONLY these views \(.*?\): (.+)\.\n", prompt)
    view = allowed.group(1).split(", ")[0] if allowed else catalog.view_names()[0]
    if view == "chat.v_sales_by_week" and re.search(r"\bby brand\b", _question(prompt), re.I):
        return SqlPlan(sql=_by_brand(_question(prompt)), explanation="fake: sales per brand, last 4 weeks")
    examples = catalog.load()["views"].get(view, {}).get("examples") or []
    own = [e["sql"] for e in examples if set(re.findall(r"chat\.\w+", e["sql"])) <= {view}]   # guard-safe
    sql = own[0] if own else f"SELECT * FROM {view}"
    return SqlPlan(sql=sql, explanation=f"fake: canned example query for {view}")


def _counted(prompt: str, result):
    """Tokens estimated from the real prompt and answer, so cost dashboards show what these
    prompts would cost (priced as the default real model, config/llm_prices.yaml)."""
    text = result.model_dump_json() if isinstance(result, BaseModel) else str(result)
    usage.record("fake", "fake", usage.estimate_tokens(prompt), usage.estimate_tokens(text))
    return result


def json_call(prompt: str, schema: type[BaseModel]) -> BaseModel:
    _behave()
    if schema is Route:
        return _counted(prompt, _route(prompt))
    if schema is SqlPlan:
        return _counted(prompt, _sql(prompt))
    raise LLMUnavailable(f"Fake LLM has no canned answer for {schema.__name__}", kind="config")


def text_call(prompt: str) -> str:
    _behave()
    rows = re.search(r"Result \((\d+) rows", prompt)
    return _counted(prompt, f"(fake answer) The query returned {rows.group(1) if rows else 'some'} rows.")
