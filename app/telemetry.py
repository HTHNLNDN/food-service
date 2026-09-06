"""Lightweight LLM-call telemetry.

Records one row per model call — tokens, grounding queries, latency, retries, outcome — into
the `llm_calls` table. Attribute names mirror the OpenTelemetry GenAI semantic conventions so
this is portable to an OTel backend later. Writing is best-effort and MUST never break a request.

Usage:
    telemetry.configure(db_path)                       # once, at startup
    with telemetry.session() as trace_id:              # group one operation's calls
        with telemetry.record("plan_week", "gemini", model, grounded=True):
            ...                                        # the HTTP layer calls set_usage()
        telemetry.link_plan(conn, trace_id, plan_id)   # attribute the calls to the saved plan
"""

import contextvars
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_DB_PATH: Path | None = None  # process-global sink target; readable from any thread
_trace: contextvars.ContextVar = contextvars.ContextVar("llm_trace", default=None)
_tenant: contextvars.ContextVar = contextvars.ContextVar("llm_tenant", default="me")
_current: contextvars.ContextVar = contextvars.ContextVar("llm_current", default=None)


def configure(db_path: Path | None) -> None:
    """Enable telemetry writes to `db_path` (or disable with None)."""
    global _DB_PATH
    _DB_PATH = db_path


@dataclass
class _Call:
    call_type: str
    provider: str
    model: str
    grounded: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    total_tokens: int | None = None
    search_queries: int = 0
    retries: int = 0
    finish_reason: str | None = None


@contextmanager
def session(tenant: str = "me"):
    """Group all LLM calls of one operation under a single trace id (yielded)."""
    t = _trace.set(uuid.uuid4().hex)
    u = _tenant.set(tenant)
    try:
        yield _trace.get()
    finally:
        _trace.reset(t)
        _tenant.reset(u)


@contextmanager
def record(call_type: str, provider: str, model: str, grounded: bool = False):
    """Wrap one semantic LLM operation; write a row (latency, outcome, accumulated usage) on exit."""
    call = _Call(call_type, provider, model, grounded)
    tok = _current.set(call)
    t0 = time.time()
    ok, error = True, None
    try:
        yield call
    except Exception as exc:  # record the failure, then re-raise
        ok, error = False, f"{type(exc).__name__}: {exc}"[:200]
        raise
    finally:
        _current.reset(tok)
        _write(call, int((time.time() - t0) * 1000), ok, error)


def set_usage(*, input_tokens=None, output_tokens=None, thinking_tokens=None,
              total_tokens=None, search_queries=0, finish_reason=None) -> None:
    """Accumulate usage onto the current call (summed across retries / JSON re-prompts)."""
    c = _current.get()
    if c is None:
        return
    if input_tokens is not None:
        c.input_tokens = (c.input_tokens or 0) + input_tokens
    if output_tokens is not None:
        c.output_tokens = (c.output_tokens or 0) + output_tokens
    if thinking_tokens is not None:
        c.thinking_tokens = (c.thinking_tokens or 0) + thinking_tokens
    if total_tokens is not None:
        c.total_tokens = (c.total_tokens or 0) + total_tokens
    c.search_queries += search_queries or 0
    if finish_reason is not None:
        c.finish_reason = finish_reason


def bump_retry() -> None:
    c = _current.get()
    if c is not None:
        c.retries += 1


def link_plan(conn: sqlite3.Connection, trace_id: str | None, plan_id: int) -> None:
    """Attribute a trace's calls to the plan they produced (called after the plan persists)."""
    if trace_id is None:
        return
    try:
        conn.execute("UPDATE llm_calls SET plan_id = ? WHERE trace_id = ? AND plan_id IS NULL",
                     (plan_id, trace_id))
        conn.commit()
    except sqlite3.Error:
        pass


def _write(call: _Call, latency_ms: int, ok: bool, error: str | None) -> None:
    if _DB_PATH is None:
        return
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO llm_calls (ts, tenant_id, trace_id, call_type, provider, model, grounded, "
            "input_tokens, output_tokens, thinking_tokens, total_tokens, search_queries, retries, "
            "latency_ms, ok, finish_reason, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(UTC).isoformat(), _tenant.get(), _trace.get(), call.call_type,
             call.provider, call.model, int(call.grounded), call.input_tokens, call.output_tokens,
             call.thinking_tokens, call.total_tokens, call.search_queries, call.retries,
             latency_ms, int(ok), call.finish_reason, error),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass  # telemetry must never break the app
