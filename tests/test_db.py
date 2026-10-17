"""Postgres-specific guarantees: migrations, transactions, concurrency, failure handling."""

import os
import subprocess
import sys
import threading
import time
from decimal import Decimal

import psycopg
import pytest
from alembic import command
from fastapi.testclient import TestClient

from app import planner, telemetry, ui
from app.agent import Recipe
from app.db import APP_TABLES, connect, make_pool
from app.main import app
from app.nutrition import Ingredient, Macros
from app.planner import PlannedRecipe
from app.services import Services
from app.shopping import _cache_put
from tests.conftest import ROOT, alembic_config


def _tables(conn) -> set[str]:
    return {r["table_name"] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")}


def test_init_creates_every_app_table_and_seeds_prices(conn):
    assert _tables(conn) == set(APP_TABLES) | {"alembic_version"}
    assert conn.execute("SELECT COUNT(*) n FROM model_prices").fetchone()["n"] >= 1


def test_migration_round_trip(pg_url, conn):
    cfg = alembic_config(pg_url)
    command.downgrade(cfg, "base")
    assert _tables(conn) == {"alembic_version"}
    command.upgrade(cfg, "head")
    assert _tables(conn) == set(APP_TABLES) | {"alembic_version"}


def test_concurrent_upgrades_both_succeed(pg_url, conn):
    """Two processes (e.g. a container restart loop) migrating at once: the advisory lock in
    migrations/env.py makes the second wait, then find the schema already at head."""
    command.downgrade(alembic_config(pg_url), "base")
    env = {**os.environ, "DATABASE_URL": pg_url}
    procs = [subprocess.Popen([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
             for _ in range(2)]
    outputs = [p.communicate(timeout=60)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outputs
    assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [
        {"version_num": "0001"}
    ]


# --- transactions & concurrency (spec: Transactions) ---


def _planned(title: str) -> PlannedRecipe:
    return PlannedRecipe(title, 2, [Ingredient("rice", 100)], ["Cook."], Macros(500, 30, 50, 10), False)


def test_plan_save_is_atomic(conn, monkeypatch):
    """A failure partway through saving a plan leaves no plan, recipe or ingredient rows."""
    real = planner._insert_recipes

    def fail_after_first_dish(c, plan_id, recipes):
        real(c, plan_id, recipes[:1])
        raise RuntimeError("fail mid-save")

    monkeypatch.setattr(planner, "_insert_recipes", fail_after_first_dish)
    with pytest.raises(RuntimeError):
        planner._persist(conn, [_planned("A"), _planned("B")])
    for table in ("plans", "recipes", "recipe_ingredients"):
        assert conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"] == 0


def test_concurrent_cache_upserts_keep_one_row(db):
    """Several writers upserting the same cache key at once: no error, exactly one row."""
    errors: list[Exception] = []

    def put():
        try:
            with connect(db) as c:
                for _ in range(25):
                    _cache_put(c, "2026-W42", "Tomato", ("tomat", Decimal(5), "rema"))
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=put) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with connect(db) as c:
        assert c.execute("SELECT COUNT(*) n FROM offer_match_cache").fetchone()["n"] == 1


class _BlockingAgent:
    """plan_week waits until released, like a slow LLM call, so the test can look at the DB."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def plan_week(self, ctx):
        self.entered.set()
        self.release.wait(timeout=10)
        return [Recipe("Rice bowl", 2, [Ingredient("rice", 100)], ["Cook."])]


class _Nutrition:
    def lookup(self, name):
        return (1, Macros(500, 30, 50, 10))


class _NoOffers:
    def offers(self, selected):
        return []


def test_no_idle_in_transaction_during_background_job(db):
    """While the background planner waits on the LLM, none of the app's connections may sit
    inside an open transaction (that would hold locks for minutes once there are many users)."""
    agent = _BlockingAgent()
    with TestClient(app) as client:
        client.app.state.build_services = lambda config, conn: Services(
            agent, _NoOffers(), _Nutrition(), None)
        client.post("/profile", data={"servings": "2", "stores": ["rema"]}, follow_redirects=False)
        client.post("/plan", follow_redirects=False)  # background thread (plan_sync is off)
        assert agent.entered.wait(timeout=10)
        try:
            with connect(db) as c:
                idle = c.execute(
                    "SELECT COUNT(*) n FROM pg_stat_activity WHERE datname = current_database() "
                    "AND state = 'idle in transaction'").fetchone()["n"]
        finally:
            agent.release.set()
        deadline = time.monotonic() + 10
        while ui._plan_state["running"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not ui._plan_state["running"] and ui._plan_state["error"] is None
        assert "Rice bowl" in client.get("/").text
    assert idle == 0


# --- failure handling (spec: Error handling) ---


def test_db_outage_renders_503(db, monkeypatch):
    with TestClient(app) as client:
        def down(*args, **kwargs):
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(client.app.state.pool, "getconn", down)
        resp = client.get("/")
    assert resp.status_code == 503
    assert "Database unavailable" in resp.text


def test_telemetry_against_dead_db_never_raises():
    pool = make_pool("postgresql://nobody@127.0.0.1:1/none")  # nothing listens on port 1
    pool.open(wait=False)
    telemetry.configure(pool)
    try:
        with telemetry.session(), telemetry.record("plan_week", "test", "m"):
            pass  # the write fails after the block; it must be swallowed, not raised
    finally:
        telemetry.configure(None)
        pool.close()
