"""Shared HTTP helpers for LLM calls: retry on transient upstream errors.

Transient failures (429, 5xx, connection/timeouts) are retried with backoff; other 4xx
(bad request, auth, unknown model) fail fast. Used by the OpenAI-compatible agent/picker
and the Gemini-native grounded agent.
"""

import json
import re
import time
from collections.abc import Callable

import httpx

from app import telemetry

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def extract_json_object(content: str) -> dict:
    """Extract a JSON object from LLM output, tolerating a ```json fence and prose/citations
    wrapped around it (grounded responses do this). Returns {} if nothing parses to a dict."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    span = re.search(r"\{.*\}", text, re.DOTALL)
    for candidate in (text, span.group() if span else None):
        if candidate is None:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, httpx.TransportError)


def with_retry[T](fn: Callable[[], T], *, attempts: int = 4, sleep: Callable[[float], None] = time.sleep) -> T:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not _is_transient(exc):
                raise
            last = exc
            telemetry.bump_retry()
        if i < attempts - 1:
            sleep(2**i)  # 1s, 2s, 4s
    assert last is not None
    raise last


def chat_completion(
    client: httpx.Client,
    model: str,
    messages: list[dict],
    *,
    attempts: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    def call() -> str:
        resp = client.post("/chat/completions", json={"model": model, "messages": messages})
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        choice = data["choices"][0]
        telemetry.set_usage(
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            finish_reason=choice.get("finish_reason"),
        )
        return choice["message"]["content"]

    return with_retry(call, attempts=attempts, sleep=sleep)
