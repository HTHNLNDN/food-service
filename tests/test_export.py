from fastapi.testclient import TestClient

from app.db import bootstrap, connect
from app.main import app
from app.nutrition import Ingredient, Macros
from app.planner import PlannedRecipe, _persist
from app.profile import Preferences, export_all, save_preferences, save_rating


def _seed(conn):
    save_preferences(conn, Preferences(600, 40, "PCOS", 2, ["rema"]))
    _persist(conn, [PlannedRecipe("Chicken bowl", 4, [Ingredient("chicken", 400)],
                                  ["Grill it"], Macros(500, 45, 10, 15), False)])
    save_rating(conn, "Chicken bowl", 1, ["chicken"])


def test_export_all_contains_profile_plans_and_ratings(tmp_path):
    conn = connect(tmp_path / "e.db")
    bootstrap(conn)
    _seed(conn)
    data = export_all(conn)
    assert data["preferences"]["max_kcal"] == 600
    assert data["preferences"]["stores"] == ["rema"]
    assert data["plans"][0]["recipes"][0]["title"] == "Chicken bowl"
    assert data["plans"][0]["recipes"][0]["ingredients"][0]["name"] == "chicken"
    assert data["ratings"][0]["title"] == "Chicken bowl"


def test_export_includes_translated_fields_when_present(tmp_path):
    conn = connect(tmp_path / "e.db")
    bootstrap(conn)
    save_preferences(conn, Preferences(600, 40, "PCOS", 2, ["rema"], language="Danish"))
    _persist(conn, [PlannedRecipe(
        "Chicken bowl", 4, [Ingredient("chicken", 400)], ["Grill it"],
        Macros(500, 45, 10, 15), False,
        title_translated="Kyllingeskål", steps_translated=["Steg det"],
        ingredient_translations={"chicken": "kylling"},
    )])
    data = export_all(conn)
    recipe = data["plans"][0]["recipes"][0]
    assert recipe["title_translated"] == "Kyllingeskål"
    assert recipe["steps_translated"] == ["Steg det"]
    assert recipe["ingredients"][0]["name_translated"] == "kylling"


def test_export_translated_fields_are_null_when_no_language_configured(tmp_path):
    conn = connect(tmp_path / "e.db")
    bootstrap(conn)
    _seed(conn)
    data = export_all(conn)
    recipe = data["plans"][0]["recipes"][0]
    assert recipe["title_translated"] is None
    assert recipe["steps_translated"] is None
    assert recipe["ingredients"][0]["name_translated"] is None


def test_full_json_export_endpoint():
    with TestClient(app) as client:
        _seed(connect(client.app.state.config.db_path))
        resp = client.get("/me/all.json")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"preferences", "plans", "ratings"}
    assert body["ratings"][0]["title"] == "Chicken bowl"


def test_ratings_csv_endpoint():
    with TestClient(app) as client:
        _seed(connect(client.app.state.config.db_path))
        resp = client.get("/me/ratings.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "Chicken bowl" in resp.text
    assert resp.text.splitlines()[0].startswith("created_at")


def test_database_file_download():
    with TestClient(app) as client:
        resp = client.get("/me/database.db")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"


def test_print_page_lists_recipes_with_steps():
    with TestClient(app) as client:
        _seed(connect(client.app.state.config.db_path))
        resp = client.get("/print")
    assert resp.status_code == 200
    assert "Chicken bowl" in resp.text
    assert "Grill it" in resp.text
