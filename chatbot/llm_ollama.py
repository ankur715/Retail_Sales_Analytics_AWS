"""Self-hosted model through Ollama (milestone 5, optional): no API key, no per-token charge, data
never leaves the machine. Same interface as the other providers.

  OLLAMA_URL     e.g. http://ollama:11434 (the `ai` compose profile)
  OLLAMA_MODEL   default qwen2.5:1.5b -- small enough for a 2-core / 8 GB Codespace on CPU

Structured output uses Ollama's `format` = the Pydantic model's JSON schema. Token counts come
from Ollama's prompt_eval_count / eval_count and are priced at 0 (config/llm_prices.yaml).
A model this small writes noticeably worse SQL than Claude: the eval quantifies the gap.
"""
import requests
from pydantic import BaseModel, ValidationError

from chatbot import usage
from chatbot.llm_types import LLMUnavailable
from etl import config

TIMEOUT_S = 180   # CPU inference on a small machine is slow


def model_name() -> str:
    return f"ollama/{config.OLLAMA_MODEL}"


def _chat(prompt: str, fmt: dict | None = None) -> str:
    body = {"model": config.OLLAMA_MODEL, "stream": False, "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt}]}
    if fmt:
        body["format"] = fmt
    try:
        r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=body, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        raise LLMUnavailable(f"Can't reach Ollama at {config.OLLAMA_URL}: {type(e).__name__}") from e
    if r.status_code == 404:
        raise LLMUnavailable(f"Ollama has no model {config.OLLAMA_MODEL!r}: ollama pull it first", kind="config")
    if r.status_code != 200:
        raise LLMUnavailable(f"Ollama error {r.status_code}", kind="busy")
    data = r.json()
    usage.record("ollama", model_name(), data.get("prompt_eval_count", 0), data.get("eval_count", 0))
    return data["message"]["content"]


def json_call(prompt: str, schema: type[BaseModel]) -> BaseModel:
    text = _chat(prompt, schema.model_json_schema())
    try:
        return schema.model_validate_json(text)
    except ValidationError as e:
        raise LLMUnavailable(f"Ollama returned output that doesn't match {schema.__name__}", kind="busy") from e


def text_call(prompt: str) -> str:
    return _chat(prompt).strip()
