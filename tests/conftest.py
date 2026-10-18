import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher

from app.auth import username_hash
from app.db import APP_TABLES, connect

ROOT = Path(__file__).resolve().parent.parent

# Points at nothing (port 1): a test that touches the DB without the `db` fixture fails fast
# instead of reaching a DATABASE_URL loaded from a developer's .env (load_dotenv never overrides).
_NO_DB = "postgresql://no-db-fixture@127.0.0.1:1/none"
HEAD = "0002"  # latest migration revision

# Every clean test DB has this account (id 1, thanks to RESTART IDENTITY); `login()` signs in as it.
TEST_USER_ID = 1
TEST_USERNAME = "tester"
TEST_PASSWORD = "correct horse battery"
TEST_USERNAME_KEY = "ab" * 32  # USERNAME_KEY for tests (32 bytes, hex)
TEST_SIGNUP_CODE = "family-invite"
_TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)  # hashed once per session, not per test


def login(client, username: str = TEST_USERNAME, password: str = TEST_PASSWORD):
    """Sign a TestClient in (its cookie jar keeps the session)."""
    resp = client.post("/login", data={"username": username, "password": password,
                                       "remember": "1"}, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return resp


def add_user(conn, username: str, password: str = TEST_PASSWORD) -> int:
    """Another account (for per-user isolation tests), stored the way sign-up stores it."""
    pw_hash = _TEST_HASH if password == TEST_PASSWORD else PasswordHasher().hash(password)
    name_hash = username_hash(bytes.fromhex(TEST_USERNAME_KEY), username)
    return conn.execute("INSERT INTO users (username_hash, password_hash) VALUES (%s, %s) "
                        "RETURNING id", (name_hash, pw_hash)).fetchone()["id"]


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Keep every test away from real data: a throwaway USDA path and no real database."""
    monkeypatch.setenv("USDA_DB_PATH", str(tmp_path / "usda.db"))
    monkeypatch.setenv("DATABASE_URL", _NO_DB)
    monkeypatch.setenv("USERNAME_KEY", TEST_USERNAME_KEY)
    monkeypatch.setenv("SIGNUP_CODE", TEST_SIGNUP_CODE)
    # process-wide state that would leak between tests: login lockouts, per-user planning jobs
    from app import auth, ui
    auth.limiter.reset_all()
    auth.signup_limiter.reset_all()
    with ui._plan_lock:
        ui._plan_states.clear()


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
    restored, the `tester` account created, and DATABASE_URL pointed at it."""
    with connect(pg_url) as c:
        if not c.execute("SELECT to_regclass('model_prices') t").fetchone()["t"]:
            command.upgrade(alembic_config(pg_url), "head")  # a test downgraded the schema
        c.execute(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE")
        c.cursor().executemany(
            "INSERT INTO model_prices (model, input_per_mtok, output_per_mtok, search_per_ktok, updated) "
            "VALUES (%(model)s, %(input_per_mtok)s, %(output_per_mtok)s, %(search_per_ktok)s, %(updated)s)",
            _seed,
        )
        assert add_user(c, TEST_USERNAME) == TEST_USER_ID
    monkeypatch.setenv("DATABASE_URL", pg_url)
    yield pg_url


@pytest.fixture
def conn(db):
    """An autocommit connection to the clean test database."""
    c = connect(db)
    yield c
    c.close()
