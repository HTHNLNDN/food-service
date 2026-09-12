from fastapi.testclient import TestClient

from app.db import bootstrap, connect
from app.main import app
from app.profile import (
    CHAINS,
    Preferences,
    load_preferences,
    save_preferences,
    selected_store_slugs,
)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    bootstrap(conn)
    return conn


# --- repository ---


def test_defaults_when_empty(tmp_path):
    prefs = load_preferences(_conn(tmp_path))
    assert prefs.servings == 2
    assert prefs.stores == []
    assert prefs.max_kcal is None
    assert prefs.min_protein_g is None
    assert prefs.restrictions == ""


def test_save_and_load_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    save_preferences(
        conn,
        Preferences(max_kcal=600, min_protein_g=40, restrictions="PCOS, no nuts",
                    servings=2, stores=["rema", "netto"]),
    )
    prefs = load_preferences(conn)
    assert prefs.max_kcal == 600
    assert prefs.min_protein_g == 40
    assert prefs.restrictions == "PCOS, no nuts"
    assert prefs.servings == 2
    assert prefs.stores == ["netto", "rema"]  # returned sorted


def test_save_replaces_previous_state(tmp_path):
    conn = _conn(tmp_path)
    save_preferences(conn, Preferences(500, 30, "", 2, ["rema"]))
    save_preferences(conn, Preferences(700, 45, "vegan", 3, ["netto", "lidl"]))
    prefs = load_preferences(conn)
    assert prefs.max_kcal == 700
    assert prefs.servings == 3
    assert prefs.stores == ["lidl", "netto"]  # replaced, not appended


def test_selected_store_slugs_is_the_whitelist(tmp_path):
    conn = _conn(tmp_path)
    save_preferences(conn, Preferences(None, None, "", 2, ["rema"]))
    assert selected_store_slugs(conn) == ["rema"]


def test_language_defaults_empty_and_roundtrips(tmp_path):
    conn = _conn(tmp_path)
    assert load_preferences(conn).language == ""  # empty = English, no translation
    save_preferences(conn, Preferences(None, None, "", 2, ["rema"], language="Danish"))
    assert load_preferences(conn).language == "Danish"


def test_chains_cover_expected_dk_slugs():
    slugs = {slug for slug, _ in CHAINS}
    assert {"rema", "netto", "foetex", "lidl", "fakta-tyskland"} <= slugs
    assert len(CHAINS) == 23


# --- HTTP ---


def test_post_profile_persists_and_export_returns_json():
    with TestClient(app) as client:
        resp = client.post(
            "/profile",
            data={
                "max_kcal": "600",
                "min_protein_g": "40",
                "restrictions": "PCOS",
                "servings": "2",
                "stores": ["rema", "netto"],
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        export = client.get("/me/export")
    assert export.status_code == 200
    body = export.json()
    assert body["max_kcal"] == 600
    assert body["min_protein_g"] == 40
    assert body["restrictions"] == "PCOS"
    assert sorted(body["stores"]) == ["netto", "rema"]


def test_post_profile_redirects_with_saved_flag():
    with TestClient(app) as client:
        resp = client.post("/profile", data={"servings": "2"}, follow_redirects=False)
    assert resp.headers["location"] == "/profile?saved=1"


def test_save_toast_mentions_translation_timing_only_when_language_set():
    with TestClient(app) as client:
        client.post("/profile", data={"servings": "2", "language": "Danish"}, follow_redirects=False)
        with_lang = client.get("/profile?saved=1").text
        client.post("/profile", data={"servings": "2", "language": ""}, follow_redirects=False)
        without_lang = client.get("/profile?saved=1").text
    assert "takes effect next time you plan" in with_lang
    assert "takes effect next time you plan" not in without_lang
    assert 'id="toast"' in with_lang and 'id="toast"' in without_lang  # save confirmation either way


def test_no_toast_without_saved_flag():
    with TestClient(app) as client:
        html = client.get("/profile").text
    assert 'id="toast"' not in html


def test_post_profile_ignores_unknown_store_slug():
    with TestClient(app) as client:
        client.post(
            "/profile",
            data={"servings": "2", "stores": ["rema", "not-a-real-chain"]},
            follow_redirects=False,
        )
        body = client.get("/me/export").json()
    assert body["stores"] == ["rema"]  # junk slug dropped


# --- bans (add, persist, remove) ---


def test_bans_save_and_load_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    save_preferences(conn, Preferences(600, 40, "", 2, ["rema"], ["fish", "mushroom"]))
    assert load_preferences(conn).bans == ["fish", "mushroom"]


def test_post_profile_persists_bans_and_lowercases_custom():
    with TestClient(app) as client:
        client.post(
            "/profile",
            data={"servings": "2", "bans": ["fish", "dairy"], "bans_custom": "mushrooms, Salmon"},
            follow_redirects=False,
        )
        bans = client.get("/me/export").json()["bans"]
    assert set(bans) == {"fish", "dairy", "mushrooms", "salmon"}  # categories + lowercased custom


def test_unchecking_a_ban_removes_it():
    with TestClient(app) as client:
        client.post("/profile", data={"servings": "2", "bans": ["fish", "dairy"], "bans_custom": "salmon"},
                    follow_redirects=False)
        # re-save with fish unchecked (salmon custom kept as its own chip)
        client.post("/profile", data={"servings": "2", "bans": ["dairy", "salmon"]}, follow_redirects=False)
        bans = client.get("/me/export").json()["bans"]
    assert "fish" not in bans
    assert set(bans) == {"dairy", "salmon"}


def test_custom_bans_render_as_removable_chips():
    with TestClient(app) as client:
        client.post("/profile", data={"servings": "2", "bans_custom": "mushrooms"}, follow_redirects=False)
        html = client.get("/profile").text
    assert 'value="mushrooms"' in html   # custom ban is a (checked) chip
    assert 'value="fish"' in html         # category chips present
