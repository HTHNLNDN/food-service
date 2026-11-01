from fastapi.testclient import TestClient

from app.main import app
from app.profile import (
    CHAINS,
    Preferences,
    load_preferences,
    save_preferences,
    selected_store_slugs,
)
from tests.conftest import TEST_USER_ID, login

# --- repository ---


def test_defaults_when_empty(conn):
    prefs = load_preferences(conn, TEST_USER_ID)
    assert prefs.servings == 2
    assert prefs.stores == []
    assert prefs.max_kcal is None
    assert prefs.min_protein_g is None
    assert prefs.restrictions == ""


def test_save_and_load_roundtrip(conn):
    save_preferences(
        conn,
        TEST_USER_ID,
        Preferences(max_kcal=600, min_protein_g=40, restrictions="PCOS, no nuts",
                    servings=2, stores=["rema", "netto"]),
    )
    prefs = load_preferences(conn, TEST_USER_ID)
    assert prefs.max_kcal == 600
    assert prefs.min_protein_g == 40
    assert prefs.restrictions == "PCOS, no nuts"
    assert prefs.servings == 2
    assert prefs.stores == ["netto", "rema"]  # returned sorted


def test_save_replaces_previous_state(conn):
    save_preferences(conn, TEST_USER_ID, Preferences(500, 30, "", 2, ["rema"]))
    save_preferences(conn, TEST_USER_ID, Preferences(700, 45, "vegan", 3, ["netto", "lidl"]))
    prefs = load_preferences(conn, TEST_USER_ID)
    assert prefs.max_kcal == 700
    assert prefs.servings == 3
    assert prefs.stores == ["lidl", "netto"]  # replaced, not appended


def test_selected_store_slugs_is_the_whitelist(conn):
    save_preferences(conn, TEST_USER_ID, Preferences(None, None, "", 2, ["rema"]))
    assert selected_store_slugs(conn, TEST_USER_ID) == ["rema"]


def test_language_defaults_empty_and_roundtrips(conn):
    assert load_preferences(conn, TEST_USER_ID).language == ""  # empty = English, no translation
    save_preferences(conn, TEST_USER_ID, Preferences(None, None, "", 2, ["rema"], language="Danish"))
    assert load_preferences(conn, TEST_USER_ID).language == "Danish"


def test_chains_cover_expected_dk_slugs():
    slugs = {slug for slug, _ in CHAINS}
    assert {"rema", "netto", "foetex", "lidl", "fakta-tyskland"} <= slugs
    assert len(CHAINS) == 23


# --- HTTP ---


def test_post_profile_persists_and_export_returns_json(db):
    with TestClient(app) as client:
        login(client)
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


def test_post_profile_redirects_with_saved_flag(db):
    with TestClient(app) as client:
        login(client)
        resp = client.post("/profile", data={"servings": "2"}, follow_redirects=False)
    assert resp.headers["location"] == "/profile?saved=1"


def test_save_toast_mentions_translation_timing_only_when_language_set(db):
    with TestClient(app) as client:
        login(client)
        client.post("/profile", data={"servings": "2", "language": "Danish"}, follow_redirects=False)
        with_lang = client.get("/profile?saved=1").text
        client.post("/profile", data={"servings": "2", "language": ""}, follow_redirects=False)
        without_lang = client.get("/profile?saved=1").text
    assert "takes effect next time you plan" in with_lang
    assert "takes effect next time you plan" not in without_lang
    assert 'id="toast"' in with_lang and 'id="toast"' in without_lang  # save confirmation either way


def test_no_toast_without_saved_flag(db):
    with TestClient(app) as client:
        login(client)
        html = client.get("/profile").text
    assert 'id="toast"' not in html


def test_post_profile_ignores_unknown_store_slug(db):
    with TestClient(app) as client:
        login(client)
        client.post(
            "/profile",
            data={"servings": "2", "stores": ["rema", "not-a-real-chain"]},
            follow_redirects=False,
        )
        body = client.get("/me/export").json()
    assert body["stores"] == ["rema"]  # junk slug dropped


# --- bans (add, persist, remove) ---


def test_bans_save_and_load_roundtrip(conn):
    save_preferences(conn, TEST_USER_ID, Preferences(600, 40, "", 2, ["rema"], ["fish", "mushroom"]))
    assert load_preferences(conn, TEST_USER_ID).bans == ["fish", "mushroom"]


def test_post_profile_persists_bans_and_lowercases_custom(db):
    with TestClient(app) as client:
        login(client)
        client.post(
            "/profile",
            data={"servings": "2", "bans": ["fish", "dairy"], "bans_custom": "mushrooms, Salmon"},
            follow_redirects=False,
        )
        bans = client.get("/me/export").json()["bans"]
    assert set(bans) == {"fish", "dairy", "mushrooms", "salmon"}  # categories + lowercased custom


def test_unchecking_a_ban_removes_it(db):
    with TestClient(app) as client:
        login(client)
        client.post("/profile", data={"servings": "2", "bans": ["fish", "dairy"], "bans_custom": "salmon"},
                    follow_redirects=False)
        # re-save with fish unchecked (salmon custom kept as its own chip)
        client.post("/profile", data={"servings": "2", "bans": ["dairy", "salmon"]}, follow_redirects=False)
        bans = client.get("/me/export").json()["bans"]
    assert "fish" not in bans
    assert set(bans) == {"dairy", "salmon"}


def test_custom_bans_render_as_removable_chips(db):
    with TestClient(app) as client:
        login(client)
        client.post("/profile", data={"servings": "2", "bans_custom": "mushrooms"}, follow_redirects=False)
        html = client.get("/profile").text
    assert 'value="mushrooms"' in html   # custom ban is a (checked) chip
    assert 'value="fish"' in html         # category chips present


# --- safe stops on Preferences ---


def _post_targets(client, kcal, protein, **extra):
    data = {"max_kcal": kcal, "min_protein_g": protein, "servings": "2", "stores": ["rema"],
            **extra}
    return client.post("/profile", data=data, follow_redirects=False)


def _saved(client):
    body = client.get("/me/export").json()
    return body["max_kcal"], body["min_protein_g"]


def test_below_the_calorie_floor_is_never_saved(db):
    with TestClient(app) as client:
        login(client)
        for extra in ({}, {"targets_choice": "keep"}):  # not even when forced
            resp = _post_targets(client, "299", "", **extra)
            assert resp.status_code == 422
            assert "300 kcal" in resp.text and "400 kcal" in resp.text
        assert _saved(client) == (None, None)


def test_impossible_combination_is_refused_even_with_keep(db):
    with TestClient(app) as client:
        login(client)
        resp = _post_targets(client, "100", "40", targets_choice="keep")
        assert resp.status_code == 422 and "160 kcal" in resp.text
        assert 'value="keep"' not in resp.text  # no "keep mine" for impossible
        assert _saved(client) == (None, None)


def test_unrealistic_needs_a_choice_and_offers_two_alternatives(db):
    with TestClient(app) as client:
        login(client)
        resp = _post_targets(client, "400", "50")
        assert resp.status_code == 422
        assert 'value="700/50"' in resp.text and 'value="400/30"' in resp.text
        assert 'value="keep"' in resp.text
        assert _saved(client) == (None, None)


def test_choosing_an_alternative_saves_it(db):
    with TestClient(app) as client:
        login(client)
        resp = _post_targets(client, "400", "50", targets_choice="700/50")
        assert resp.status_code == 303
        assert _saved(client) == (700, 50)


def test_keep_mine_saves_unrealistic_targets(db):
    with TestClient(app) as client:
        login(client)
        assert _post_targets(client, "400", "50", targets_choice="keep").status_code == 303
        assert _saved(client) == (400, 50)
        assert _post_targets(client, "300", "", targets_choice="keep").status_code == 303
        assert _saved(client) == (300, None)  # 300 itself is allowed


def test_a_forged_choice_cannot_sneak_past_the_floor(db):
    with TestClient(app) as client:
        login(client)
        resp = _post_targets(client, "400", "50", targets_choice="200/10")
        assert resp.status_code == 422
        assert _saved(client) == (None, None)


def test_ambitious_saves_and_shows_a_note(db):
    with TestClient(app) as client:
        login(client)
        assert _post_targets(client, "400", "38").status_code == 303
        assert _saved(client) == (400, 38)
        assert "Protein-heavy" in client.get("/profile").text


def test_rejected_form_keeps_the_other_fields(db):
    with TestClient(app) as client:
        login(client)
        resp = _post_targets(client, "400", "50", restrictions="no spicy food")
        assert 'value="no spicy food"' in resp.text


def test_kept_targets_are_not_rechallenged_on_later_saves(db):
    with TestClient(app) as client:
        login(client)
        assert _post_targets(client, "400", "50", targets_choice="keep").status_code == 303
        data = {"max_kcal": "400", "min_protein_g": "50", "servings": "2", "stores": ["netto"]}
        resp = client.post("/profile", data=data, follow_redirects=False)
        assert resp.status_code == 303
        assert _saved(client) == (400, 50)
        assert client.get("/me/export").json()["stores"] == ["netto"]


def test_a_different_unrealistic_pair_still_needs_a_choice(db):
    with TestClient(app) as client:
        login(client)
        assert _post_targets(client, "400", "50", targets_choice="keep").status_code == 303
        assert _post_targets(client, "400", "45").status_code == 422
        assert _saved(client) == (400, 50)


def test_kept_targets_show_a_calm_notice_not_an_error(db):
    with TestClient(app) as client:
        login(client)
        assert _post_targets(client, "400", "50", targets_choice="keep").status_code == 303
        html = client.get("/profile").text
        assert 'class="notice targets-check"' in html
        assert 'class="error targets-check"' not in html


def test_calorie_field_reads_as_a_target_not_a_maximum(db):
    with TestClient(app) as client:
        login(client)
        page = client.get("/profile").text
    assert "Calories per serving" in page
    assert "Max calories" not in page
    assert "give or take 10%" in page
