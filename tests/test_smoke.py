from fastapi.testclient import TestClient

from app.db import bootstrap, connect
from app.main import app


def test_root_ok():
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "food-service" in resp.text


def test_db_bootstrap_idempotent(tmp_path):
    db = tmp_path / "test.db"
    conn = connect(db)
    bootstrap(conn)
    bootstrap(conn)  # second run must not error
    assert db.exists()
    conn.close()
