from alembic import command
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import alembic_config


def test_root_ok(db):
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "food-service" in resp.text


def test_migrations_idempotent(conn, pg_url):
    command.upgrade(alembic_config(pg_url), "head")  # already at head: must be a no-op, not an error
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "0001"
