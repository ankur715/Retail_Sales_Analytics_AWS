"""Tokens and cost per LLM call and per question (milestone 5).

Every provider calls record() after each model call with the tokens the provider reported
(Bedrock and Gemini return exact counts; the fake model estimates ~4 characters per token from
the real prompt). Each call is priced from config/llm_prices.yaml and:
  - counted in Prometheus: chatbot_llm_tokens_total{provider,model,step,direction},
    chatbot_llm_cost_usd_total{provider,model,step}  -> cost and tokens in Grafana
  - added to the current question's running total (a context variable), which the chatbot
    returns as `usage` in the response and observes as chatbot_request_cost_usd

SQL generation runs in the sqlgen service: it records its own metrics there and sends its
usage back in the gRPC reply, which the chatbot adds to the question's total (add()).
Unknown models are counted as tokens with cost 0 and logged once, never guessed.
"""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from prometheus_client import Counter, Histogram

log = logging.getLogger(__name__)
PRICES_PATH = Path(__file__).resolve().parent.parent / "config" / "llm_prices.yaml"

TOKENS = Counter("chatbot_llm_tokens_total", "LLM tokens", ["provider", "model", "step", "direction"])
COST = Counter("chatbot_llm_cost_usd_total", "LLM cost in USD at list prices", ["provider", "model", "step"])
REQUEST_COST = Histogram("chatbot_request_cost_usd", "LLM cost of one answered question (cache misses)",
                         buckets=(0.0005, 0.001, 0.002, 0.004, 0.008, 0.015, 0.03, 0.06, 0.12))


@dataclass
class Usage:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cost_usd"] = round(self.cost_usd, 6)
        return d


_current: ContextVar[Usage | None] = ContextVar("llm_usage", default=None)
_step: ContextVar[str] = ContextVar("llm_step", default="other")


@lru_cache(maxsize=1)
def _price_table() -> list[dict]:
    return yaml.safe_load(PRICES_PATH.read_text())["models"]


@lru_cache(maxsize=64)
def price(model: str) -> tuple[float, float] | None:
    """(input, output) USD per million tokens, or None if the model isn't priced."""
    table = _price_table()
    for row in table:
        if row["match"].lower() in model.lower():
            if "priced_as" in row:
                return price(row["priced_as"])
            return float(row["input"]), float(row["output"])
    log.warning("no price for model %r: counting tokens with cost 0 (add it to %s)", model, PRICES_PATH.name)
    return None


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p = price(model)
    return 0.0 if p is None else (input_tokens * p[0] + output_tokens * p[1]) / 1_000_000


@contextmanager
def step(name: str):
    """Label the LLM calls made inside (route / write_sql / summarize / judge)."""
    token = _step.set(name)
    try:
        yield
    finally:
        _step.reset(token)


def record(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """Count one model call; returns its cost."""
    usd = cost(model, input_tokens, output_tokens)
    s = _step.get()
    TOKENS.labels(provider, model, s, "input").inc(input_tokens)
    TOKENS.labels(provider, model, s, "output").inc(output_tokens)
    COST.labels(provider, model, s).inc(usd)
    add(input_tokens, output_tokens, usd)
    return usd


def add(input_tokens: int, output_tokens: int, usd: float, calls: int = 1) -> None:
    """Add to the current question's total (no metrics: e.g. usage reported back by sqlgen)."""
    current = _current.get()
    if current is not None:
        current.llm_calls += calls
        current.input_tokens += input_tokens
        current.output_tokens += output_tokens
        current.cost_usd += usd


@contextmanager
def track():
    """Collect the usage of everything inside, e.g. one question. Nested tracks roll up into the
    enclosing one (the eval tracks a question that the chatbot also tracks)."""
    parent, usage = _current.get(), Usage()
    token = _current.set(usage)
    try:
        yield usage
    finally:
        _current.reset(token)
        if parent is not None:
            add(usage.input_tokens, usage.output_tokens, usage.cost_usd, calls=usage.llm_calls)


def estimate_tokens(text: str) -> int:
    """~4 characters per token: the usual rule of thumb for English prompts (fake model only)."""
    return max(1, len(text) // 4)
