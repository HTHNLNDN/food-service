"""Lightweight LLM-call telemetry.

Records one row per model call — tokens, grounding queries, latency, retries, outcome — into
the `llm_calls` table. Attribute names mirror the OpenTelemetry GenAI semantic conventions so
this is portable to an OTel backend later. Writing is best-effort and MUST never break a request.

Usage:
    telemetry.configure(pool)                          # once, at startup
    with telemetry.session() as trace_id:              # group one operation's calls
        with telemetry.record("plan_week", "gemini", model, grounded=True):
            ...                                        # the HTTP layer calls set_usage()
        telemetry.link_plan(conn, trace_id, plan_id)   # attribute the calls to the saved plan
"""

import contextvars
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg_pool import ConnectionPool

_POOL: ConnectionPool | None = None  # process-global sink; thread-safe, shared by any thread
_WRITE_TIMEOUT_S = 2  # never let a busy/dead DB stall an LLM call for long
_trace: contextvars.ContextVar = contextvars.ContextVar("llm_trace", default=None)
_tenant: contextvars.ContextVar = contextvars.ContextVar("llm_tenant", default="me")
_current: contextvars.ContextVar = contextvars.ContextVar("llm_current", default=None)


def configure(pool: ConnectionPool | None) -> None:
    """Enable telemetry writes through `pool` (or disable with None)."""
    global _POOL
    _POOL = pool


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
def tenant(tenant_id: str):
    """Attribute every call made inside the block to `tenant_id` (the signed-in user's id)."""
    token = _tenant.set(tenant_id)
    try:
        yield
    finally:
        _tenant.reset(token)


@contextmanager
def session(tenant: str | None = None):
    """Group all LLM calls of one operation under a single trace id (yielded)."""
    t = _trace.set(uuid.uuid4().hex)
    u = _tenant.set(tenant if tenant is not None else _tenant.get())  # default: inherit
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


def link_plan(conn: psycopg.Connection, trace_id: str | None, plan_id: int) -> None:
    """Attribute a trace's calls to the plan they produced (called after the plan persists)."""
    if trace_id is None:
        return
    try:
        conn.execute("UPDATE llm_calls SET plan_id = %s WHERE trace_id = %s AND plan_id IS NULL",
                     (plan_id, trace_id))
    except psycopg.Error:
        pass


def _write(call: _Call, latency_ms: int, ok: bool, error: str | None) -> None:
    pool = _POOL
    if pool is None:
        return
    try:
        with pool.connection(timeout=_WRITE_TIMEOUT_S) as conn:
            conn.execute(
                "INSERT INTO llm_calls (ts, tenant_id, trace_id, call_type, provider, model, grounded, "
                "input_tokens, output_tokens, thinking_tokens, total_tokens, search_queries, retries, "
                "latency_ms, ok, finish_reason, error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (datetime.now(UTC), _tenant.get(), _trace.get(), call.call_type,
                 call.provider, call.model, bool(call.grounded), call.input_tokens, call.output_tokens,
                 call.thinking_tokens, call.total_tokens, call.search_queries, call.retries,
                 latency_ms, bool(ok), call.finish_reason, error),
            )
    except psycopg.Error:  # includes PoolTimeout
        pass  # telemetry must never break the app
