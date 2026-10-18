from collections.abc import Iterator
from typing import Annotated

import psycopg
from fastapi import Depends, Request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# The schema lives in Alembic migrations (migrations/versions/), never here.

# Every app table (everything but alembic_version): what tests and evals truncate for a clean slate.
APP_TABLES = (
    "_meta", "preferences", "stores", "ratings", "resolve_cache", "nutrition_cache",
    "llm_calls", "eval_runs", "eval_scores", "ui_translations", "model_prices",
    "missing_ingredients", "offers_cache", "offer_match_cache", "plans", "recipes",
    "recipe_ingredients", "users", "sessions", "user_preferences", "user_stores",
)


def connect(url: str) -> psycopg.Connection:
    """A standalone autocommit connection (scripts, evals, tests). The app uses `make_pool`.

    Autocommit, because LLM calls happen while connections are held: an implicit transaction
    would sit `idle in transaction` for minutes. Wrap multi-statement writes in
    `with conn.transaction():` instead."""
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def make_pool(url: str) -> ConnectionPool:
    """The app's connection pool (opened in the lifespan). Same connection settings as `connect`."""
    return ConnectionPool(
        url, min_size=1, max_size=10, open=False,
        timeout=5,  # a request waits at most this long for a connection, then gets the 503 page
        kwargs={"autocommit": True, "row_factory": dict_row},
        check=ConnectionPool.check_connection,  # drop connections that died (e.g. a DB restart)
    )


def get_conn(request: Request) -> Iterator[psycopg.Connection]:
    """FastAPI dependency: a pooled connection for the duration of the request."""
    with request.app.state.pool.connection() as conn:
        yield conn


# Shared FastAPI dependency annotation for route handlers.
Conn = Annotated[psycopg.Connection, Depends(get_conn)]
