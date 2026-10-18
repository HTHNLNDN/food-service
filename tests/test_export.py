from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from app.nutrition import Ingredient, Macros
from app.planner import PlannedRecipe, _persist
from app.profile import Preferences, export_all, save_preferences, save_rating
from tests.conftest import TEST_USER_ID, login


def _seed(conn):
    save_preferences(conn, TEST_USER_ID, Preferences(600, 40, "PCOS", 2, ["rema"]))
    _persist(conn, TEST_USER_ID, [PlannedRecipe("Chicken bowl", 4, [Ingredient("chicken", 400)],
                                  ["Grill it"], Macros(500, 45, 10, 15), False)])
    save_rating(conn, TEST_USER_ID, "Chicken bowl", 1, ["chicken"])


def test_export_all_contains_profile_plans_and_ratings(conn):
    _seed(conn)
    data = export_all(conn, TEST_USER_ID)
    assert data["preferences"]["max_kcal"] == 600
    assert data["preferences"]["stores"] == ["rema"]
    assert data["plans"][0]["recipes"][0]["title"] == "Chicken bowl"
    assert data["plans"][0]["recipes"][0]["ingredients"][0]["name"] == "chicken"
    assert data["ratings"][0]["title"] == "Chicken bowl"


def test_export_includes_translated_fields_when_present(conn):
    save_preferences(conn, TEST_USER_ID, Preferences(600, 40, "PCOS", 2, ["rema"], language="Danish"))
    _persist(conn, TEST_USER_ID, [PlannedRecipe(
        "Chicken bowl", 4, [Ingredient("chicken", 400)], ["Grill it"],
        Macros(500, 45, 10, 15), False,
        title_translated="Kyllingeskål", steps_translated=["Steg det"],
        ingredient_translations={"chicken": "kylling"},
    )])
    data = export_all(conn, TEST_USER_ID)
    recipe = data["plans"][0]["recipes"][0]
    assert recipe["title_translated"] == "Kyllingeskål"
    assert recipe["steps_translated"] == ["Steg det"]
    assert recipe["ingredients"][0]["name_translated"] == "kylling"


def test_export_translated_fields_are_null_when_no_language_configured(conn):
    _seed(conn)
    data = export_all(conn, TEST_USER_ID)
    recipe = data["plans"][0]["recipes"][0]
    assert recipe["title_translated"] is None
    assert recipe["steps_translated"] is None
    assert recipe["ingredients"][0]["name_translated"] is None


def test_full_json_export_endpoint(db):
    with TestClient(app) as client:
        login(client)
        _seed(connect(db))
        resp = client.get("/me/all.json")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"preferences", "plans", "ratings"}
    assert body["ratings"][0]["title"] == "Chicken bowl"


def test_ratings_csv_endpoint(db):
    with TestClient(app) as client:
        login(client)
        _seed(connect(db))
        resp = client.get("/me/ratings.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "Chicken bowl" in resp.text
    assert resp.text.splitlines()[0].startswith("created_at")


def test_raw_database_download_is_gone(db):
    """Postgres has no file to hand out, and with several users it would leak everyone's data;
    the JSON/CSV exports above cover taking your data with you."""
    with TestClient(app) as client:
        login(client)
        resp = client.get("/me/database.db")
        me = client.get("/me").text
    assert resp.status_code == 404
    assert "/me/database.db" not in me


def test_print_page_lists_recipes_with_steps(db):
    with TestClient(app) as client:
        login(client)
        _seed(connect(db))
        resp = client.get("/print")
    assert resp.status_code == 200
    assert "Chicken bowl" in resp.text
    assert "Grill it" in resp.text


def test_print_recipe_gets_compact_class_when_long(db):
    with TestClient(app) as client:
        login(client)
        conn = connect(db)
        save_preferences(conn, TEST_USER_ID, Preferences(600, 40, "PCOS", 2, ["rema"]))
        long_ingredients = [Ingredient(f"ingredient {i}", 10) for i in range(14)]
        long_steps = [f"Step {i} of the recipe." for i in range(10)]
        _persist(conn, TEST_USER_ID, [PlannedRecipe("Long dish", 4, long_ingredients, long_steps,
                                      Macros(500, 45, 10, 15), False)])
        resp = client.get("/print")
    assert resp.status_code == 200
    assert 'class="print-recipe compact"' in resp.text


def test_print_recipe_stays_normal_class_when_short(db):
    with TestClient(app) as client:
        login(client)
        _seed(connect(db))
        resp = client.get("/print")
    assert resp.status_code == 200
    assert 'class="print-recipe compact"' not in resp.text
    assert 'class="print-recipe"' in resp.text
