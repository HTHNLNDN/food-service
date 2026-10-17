import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config

from app.db import APP_TABLES, connect

ROOT = Path(__file__).resolve().parent.parent

# Points at nothing (port 1): a test that touches the DB without the `db` fixture fails fast
# instead of reaching a DATABASE_URL loaded from a developer's .env (load_dotenv never overrides).
_NO_DB = "postgresql://no-db-fixture@127.0.0.1:1/none"


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Keep every test away from real data: a throwaway USDA path and no real database."""
    monkeypatch.setenv("USDA_DB_PATH", str(tmp_path / "usda.db"))
    monkeypatch.setenv("DATABASE_URL", _NO_DB)


@pytest.fixture(scope="session")
def pg_url():
    """One Postgres 18 per test session, migrated to head. Started only when a test needs the
    DB, so DB-free test files never touch Docker.

    TEST_DATABASE_URL (CI: a `services: postgres` container) is used instead of starting one.
    Tests truncate every table, so its database name must contain "test"."""
    container = None
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        if "test" not in urlsplit(url).path:
            pytest.fail(f"TEST_DATABASE_URL must name a throwaway *test* database, got {url!r}")
    else:
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer("postgres:18-alpine", driver=None)
        try:
            container.start()
        except Exception as exc:  # noqa: BLE001 - turn any Docker problem into one clear message
            pytest.fail(f"DB tests need Docker running (could not start postgres:18-alpine): {exc}")
        url = container.get_connection_url()
    command.upgrade(alembic_config(url), "head")
    with connect(url) as c:  # what the migration seeds, restored before every test
        _seed.extend(c.execute("SELECT * FROM model_prices").fetchall())
    yield url
    if container is not None:
        container.stop()


_seed: list[dict] = []


@pytest.fixture
def db(pg_url, monkeypatch):
    """A clean database for this test: every app table emptied, the migration's price seed
    restored, and DATABASE_URL pointed at it (for TestClient / load_config)."""
    with connect(pg_url) as c:
        if not c.execute("SELECT to_regclass('model_prices') t").fetchone()["t"]:
            command.upgrade(alembic_config(pg_url), "head")  # a test downgraded the schema
        c.execute(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE")
        c.cursor().executemany(
            "INSERT INTO model_prices (model, input_per_mtok, output_per_mtok, search_per_ktok, updated) "
            "VALUES (%(model)s, %(input_per_mtok)s, %(output_per_mtok)s, %(search_per_ktok)s, %(updated)s)",
            _seed,
        )
    monkeypatch.setenv("DATABASE_URL", pg_url)
    yield pg_url


@pytest.fixture
def conn(db):
    """An autocommit connection to the clean test database."""
    c = connect(db)
    yield c
    c.close()
