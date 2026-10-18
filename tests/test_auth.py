"""Accounts, login sessions, route protection and per-user data isolation."""

import hashlib
import html
import io
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import telemetry, ui, users
from app.agent import Recipe
from app.auth import OPEN_PATHS, SESSION_COOKIE, _token_hash, limiter, safe_next, username_hash
from app.main import app
from app.nutrition import Ingredient, Macros
from app.services import Services
from tests.conftest import (
    TEST_PASSWORD,
    TEST_SIGNUP_CODE,
    TEST_USER_ID,
    TEST_USERNAME,
    TEST_USERNAME_KEY,
    add_user,
    login,
)

# --- route protection ---


def _get_routes() -> list[str]:
    """Every GET page the app serves (from its schema) plus FastAPI's built-in docs pages."""
    paths = {p for p, ops in app.openapi()["paths"].items() if "get" in ops and "{" not in p}
    paths |= {"/docs", "/redoc", "/openapi.json"}  # must not leak the API surface either
    routes = sorted(paths - OPEN_PATHS)
    assert len(routes) >= 10, routes  # the sweep must never silently cover nothing
    return routes


@pytest.mark.parametrize("path", _get_routes())
def test_every_page_requires_login(db, path):
    with TestClient(app) as client:
        resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login?next=")


def test_posts_require_login_and_change_nothing(conn):
    with TestClient(app) as client:
        for path in ("/plan", "/profile", "/plan/0/rate", "/plan/0/decline", "/logout",
                     "/account/password"):
            resp = client.post(path, data={"servings": "5"}, follow_redirects=False)
            assert resp.status_code == 303 and resp.headers["location"].startswith("/login"), path
    assert conn.execute("SELECT COUNT(*) n FROM user_preferences").fetchone()["n"] == 0


def test_login_page_and_static_are_public(db):
    with TestClient(app) as client:
        assert client.get("/login").status_code == 200
        assert client.get("/static/app.css").status_code == 200


def test_login_redirects_back_to_the_page_asked_for(db):
    with TestClient(app) as client:
        first = client.get("/shopping", follow_redirects=False)
        assert first.headers["location"] == "/login?next=/shopping"
        resp = client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD,
                                           "next": "/shopping"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/shopping"


# --- the login form (so password managers can save it) ---


def test_login_form_uses_password_manager_friendly_fields(db):
    with TestClient(app) as client:
        html = client.get("/login").text
    assert 'name="username"' in html and 'autocomplete="username"' in html
    assert 'type="password"' in html and 'autocomplete="current-password"' in html
    assert 'name="remember"' in html and "checked" in html  # stay signed in by default
    assert 'class="tabbar"' not in html  # no app nav before signing in


# --- logging in ---


def test_login_sets_a_long_lived_http_only_cookie(db):
    with TestClient(app) as client:
        resp = login(client)
        cookie = resp.headers["set-cookie"]
        assert f"{SESSION_COOKIE}=" in cookie
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert f"Max-Age={90 * 24 * 3600}" in cookie
        page = client.get("/more")
    assert page.status_code == 200 and "Sign out" in page.text


def test_unticked_keep_me_signed_in_gives_a_browser_session_cookie(conn):
    with TestClient(app) as client:
        resp = client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD},
                           follow_redirects=False)
    assert resp.status_code == 303
    assert "Max-Age" not in resp.headers["set-cookie"]  # gone when the browser closes
    row = conn.execute("SELECT persistent, expires_at FROM sessions").fetchone()
    assert row["persistent"] is False
    assert row["expires_at"] - datetime.now(UTC) <= timedelta(hours=12)


def test_username_is_case_insensitive(db):
    with TestClient(app) as client:
        login(client, username=TEST_USERNAME.upper())
        assert client.get("/").status_code == 200


def test_wrong_password_and_unknown_user_look_the_same(db):
    with TestClient(app) as client:
        wrong = client.post("/login", data={"username": TEST_USERNAME, "password": "nope-nope"})
        unknown = client.post("/login", data={"username": "nobody", "password": "nope-nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert "Wrong username or password." in wrong.text and "Wrong username or password." in unknown.text
    assert SESSION_COOKIE not in wrong.headers.get("set-cookie", "")


def test_repeated_failures_lock_the_username_out(db):
    with TestClient(app) as client:
        for _ in range(limiter.MAX):
            client.post("/login", data={"username": TEST_USERNAME, "password": "guess-guess"})
        locked = client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
        other = client.post("/login", data={"username": "someone-else", "password": "x"})
    assert locked.status_code == 429 and "Too many attempts" in locked.text  # even the right one
    assert other.status_code == 401  # only that username is locked


def test_open_redirects_are_refused():
    for evil in ("//evil.example", "https://evil.example", "/\\evil.example", "evil", "", None):
        assert safe_next(evil) == "/"
    assert safe_next("/shopping?x=1") == "/shopping?x=1"


def test_login_ignores_an_offsite_next(db):
    with TestClient(app) as client:
        resp = client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD,
                                           "next": "//evil.example/phish"}, follow_redirects=False)
    assert resp.headers["location"] == "/"


def test_cross_site_posts_are_refused(db):
    with TestClient(app) as client:
        login(client)
        evil = client.post("/plan/0/rate", data={"score": "1"}, headers={"origin": "https://evil.example"},
                           follow_redirects=False)
        same = client.post("/plan/0/rate", data={"score": "1"}, headers={"origin": "http://testserver"},
                           follow_redirects=False)
    assert evil.status_code == 403
    assert same.status_code == 303


# --- sessions ---


def test_only_a_hash_of_the_session_token_is_stored(conn):
    with TestClient(app) as client:
        login(client)
        token = client.cookies[SESSION_COOKIE]
    stored = conn.execute("SELECT token_hash FROM sessions").fetchone()["token_hash"]
    assert stored == _token_hash(token) and token not in stored


def test_logout_ends_the_session(db):
    with TestClient(app) as client:
        login(client)
        token = client.cookies[SESSION_COOKIE]
        client.post("/logout", follow_redirects=False)
        client.cookies.set(SESSION_COOKIE, token)  # replaying the old cookie must not work
        assert client.get("/", follow_redirects=False).status_code == 303


def test_expired_session_sends_you_to_login(conn):
    with TestClient(app) as client:
        login(client)
        conn.execute("UPDATE sessions SET expires_at = now() - interval '1 minute'")
        assert client.get("/", follow_redirects=False).status_code == 303


def test_persistent_session_slides_forward_when_used(conn):
    with TestClient(app) as client:
        login(client)
        conn.execute("UPDATE sessions SET last_seen_at = now() - interval '2 days', "
                     "expires_at = now() + interval '1 day'")
        assert client.get("/").status_code == 200
    left = conn.execute("SELECT expires_at - now() AS left FROM sessions").fetchone()["left"]
    assert left > timedelta(days=89)


# --- changing your password ---


def _change(client, current, new, confirm=None):
    return client.post("/account/password", data={
        "current_password": current, "new_password": new,
        "confirm_password": new if confirm is None else confirm}, follow_redirects=False)


def test_change_password_rejects_bad_input(db):
    with TestClient(app) as client:
        login(client)
        assert _change(client, "wrong-current", "brand new pw").status_code == 401
        assert _change(client, TEST_PASSWORD, "brand new pw", "different pw").status_code == 400
        assert _change(client, TEST_PASSWORD, "short").status_code == 400


def test_change_password_signs_out_other_devices_but_not_this_one(db):
    with TestClient(app) as phone, TestClient(app) as laptop:
        login(phone)
        login(laptop)
        assert _change(laptop, TEST_PASSWORD, "brand new pw").status_code == 303
        assert laptop.get("/", follow_redirects=False).status_code == 200
        assert phone.get("/", follow_redirects=False).status_code == 303
        login(phone, password="brand new pw")
        assert phone.get("/").status_code == 200


# --- per-user data isolation ---


class _Agent:
    def __init__(self, title):
        self.title = title

    def plan_week(self, ctx):
        with telemetry.record("plan_week", "fake", "fake-model"):  # an "LLM call" to attribute
            pass
        return [Recipe(self.title, 2, [Ingredient("rice", 100)], ["Cook."])]

    def replace(self, declined, ctx):
        return Recipe(f"{self.title} (new)", 2, [Ingredient("rice", 100)], ["Cook."])


class _Nutrition:
    def lookup(self, name):
        return (1, Macros(250, 30, 30, 5, 2))


class _Offers:
    def offers(self, selected):
        return []


def _fakes(client, title):
    client.app.state.build_services = lambda config, conn: Services(
        _Agent(title), _Offers(), _Nutrition(), None)
    client.app.state.plan_sync = True


def test_each_user_has_their_own_preferences_ratings_and_plans(conn):
    add_user(conn, "partner")
    with TestClient(app) as me, TestClient(app) as partner:
        login(me)
        login(partner, username="partner")
        _fakes(me, "My Rice")
        me.post("/profile", data={"servings": "4", "restrictions": "PCOS", "stores": ["rema"],
                                  "bans": ["fish"]}, follow_redirects=False)
        me.post("/plan", follow_redirects=False)
        me.post("/plan/0/rate", data={"score": "1"}, follow_redirects=False)

        assert "My Rice" in me.get("/").text
        mine = me.get("/me/all.json").json()
        theirs = partner.get("/me/all.json").json()
        their_home = partner.get("/").text
        their_cost = partner.get("/cost").text
        my_cost = me.get("/cost").text

    assert mine["preferences"]["servings"] == 4 and mine["preferences"]["bans"] == ["fish"]
    assert len(mine["plans"]) == 1 and len(mine["ratings"]) == 1
    assert theirs["preferences"] == {"max_kcal": None, "min_protein_g": None, "restrictions": "",
                                     "servings": 2, "stores": [], "bans": [], "language": ""}
    assert theirs["plans"] == [] and theirs["ratings"] == []
    assert "My Rice" not in their_home
    assert "No LLM calls logged yet" in their_cost and "No LLM calls logged yet" not in my_cost
    assert conn.execute("SELECT DISTINCT tenant_id FROM llm_calls").fetchall() == [
        {"tenant_id": str(TEST_USER_ID)}]  # the call was attributed to whoever planned


def test_a_user_cannot_touch_someone_elses_plan(conn):
    partner_id = add_user(conn, "partner")
    with TestClient(app) as me, TestClient(app) as partner:
        login(me)
        _fakes(me, "My Rice")
        me.post("/plan", follow_redirects=False)
        login(partner, username="partner")
        _fakes(partner, "Partner Dish")
        partner.post("/plan/0/rate", data={"score": "-1"}, follow_redirects=False)
        partner.post("/plan/0/decline", follow_redirects=False)
        assert "My Rice" in me.get("/").text and "(new)" not in me.get("/").text
    assert conn.execute("SELECT COUNT(*) n FROM ratings").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM plans WHERE user_id = %s",
                        (partner_id,)).fetchone()["n"] == 0


def test_one_users_planning_job_does_not_block_another(conn):
    add_user(conn, "partner")
    with ui._plan_lock:
        ui._state(TEST_USER_ID)["running"] = True  # tester's (slow) job is in flight
    with TestClient(app) as partner:
        login(partner, username="partner")
        _fakes(partner, "Partner Dish")
        partner.post("/plan", follow_redirects=False)
        assert "Partner Dish" in partner.get("/").text


# --- claiming the pre-accounts data (the only admin command) ---


def _claim(monkeypatch, username=TEST_USERNAME, password=TEST_PASSWORD):
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{username}\n{password}\n"))
    return users.main(["claim-existing", "--stdin"])


def _pre_accounts_data(conn):
    """The world before accounts: unowned rows + the single-household preference row."""
    conn.execute("INSERT INTO preferences (id, max_kcal, servings, bans) VALUES (1, 650, 3, '[\"nuts\"]')")
    conn.execute("INSERT INTO stores (chain_slug) VALUES ('netto'), ('rema')")
    conn.execute("INSERT INTO plans (created_at, week) VALUES (now(), '2026-W40')")
    conn.execute("INSERT INTO ratings (title, score, tags, created_at) VALUES ('Soup', 1, '[]', now())")
    conn.execute("INSERT INTO llm_calls (ts, call_type, provider, model, ok) "
                 "VALUES (now(), 'plan_week', 'p', 'm', TRUE)")  # tenant_id defaults to 'me'


def test_claim_existing_gives_the_old_data_to_your_account(conn, monkeypatch, capsys):
    _pre_accounts_data(conn)
    assert _claim(monkeypatch, username=TEST_USERNAME.upper()) == 0  # same account, any case
    assert "1 plans, 1 ratings" in capsys.readouterr().out
    me = TEST_USER_ID
    assert conn.execute("SELECT COUNT(*) n FROM plans WHERE user_id IS NULL").fetchone()["n"] == 0
    assert conn.execute("SELECT user_id FROM ratings").fetchone()["user_id"] == me
    prefs = conn.execute("SELECT * FROM user_preferences WHERE user_id = %s", (me,)).fetchone()
    assert prefs["max_kcal"] == 650 and prefs["servings"] == 3 and prefs["bans"] == '["nuts"]'
    assert [r["chain_slug"] for r in conn.execute(
        "SELECT chain_slug FROM user_stores WHERE user_id = %s ORDER BY 1", (me,))] == ["netto", "rema"]
    assert conn.execute("SELECT tenant_id FROM llm_calls").fetchone()["tenant_id"] == str(me)


def test_claim_needs_your_password_and_works_only_once(conn, monkeypatch, capsys):
    _pre_accounts_data(conn)
    add_user(conn, "partner")
    assert _claim(monkeypatch, password="not my password") == 1
    assert _claim(monkeypatch, username="nobody") == 1
    assert conn.execute("SELECT COUNT(*) n FROM plans WHERE user_id IS NULL").fetchone()["n"] == 1
    assert _claim(monkeypatch) == 0
    assert _claim(monkeypatch, username="partner") == 1  # can't also take the old preferences
    err = capsys.readouterr().err
    assert "wrong username or password" in err and "already claimed" in err


def test_there_is_no_admin_way_to_create_reset_or_list_accounts():
    for cmd in ("create", "set-password", "list"):
        with pytest.raises(SystemExit):
            users.main([cmd])


def test_unowned_rows_stay_invisible_until_claimed(conn):
    conn.execute("INSERT INTO plans (created_at, week) VALUES (now(), '2026-W40')")  # pre-accounts
    with TestClient(app) as client:
        login(client)
        assert client.get("/me/all.json").json()["plans"] == []


# --- sign-up (invite code) ---


def _signup(client, username="newbie", password="a fine password", confirm=None,
            code=TEST_SIGNUP_CODE, understood="1"):
    data = {"invite_code": code, "username": username, "password": password,
            "confirm_password": password if confirm is None else confirm}
    if understood:
        data["understood"] = understood
    return client.post("/signup", data=data, follow_redirects=False)


def test_signup_creates_an_account_and_signs_you_in(db):
    with TestClient(app) as client:
        form = client.get("/signup").text
        assert 'autocomplete="new-password"' in form and 'autocomplete="username"' in form
        assert "no password reset" in form.lower()
        resp = _signup(client)
        assert resp.status_code == 303 and resp.headers["location"] == "/"
        assert f"Max-Age={90 * 24 * 3600}" in resp.headers["set-cookie"]
        assert client.get("/").status_code == 200
    with TestClient(app) as again:
        login(again, username="NEWBIE", password="a fine password")  # case doesn't matter
        assert again.get("/me/all.json").json()["plans"] == []  # a fresh, empty account


@pytest.mark.parametrize(("kwargs", "status", "message"), [
    ({"code": "wrong-code"}, 403, "invite code"),
    ({"code": ""}, 403, "invite code"),
    ({"confirm": "something else"}, 400, "don't match"),
    ({"password": "short"}, 400, "at least 8"),
    ({"username": "ab"}, 400, "3-64 characters"),
    ({"username": "has space"}, 400, "no spaces"),
    ({"username": TEST_USERNAME.title()}, 400, "taken"),  # case-insensitive duplicate
    ({"understood": ""}, 400, "can't be recovered"),
])
def test_signup_refuses_bad_input(conn, kwargs, status, message):
    before = conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    with TestClient(app) as client:
        resp = _signup(client, **kwargs)
    assert resp.status_code == status and message in html.unescape(resp.text)
    assert SESSION_COOKIE not in resp.headers.get("set-cookie", "")
    assert conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == before


def test_signup_is_closed_without_an_invite_code(conn, monkeypatch):
    monkeypatch.setenv("SIGNUP_CODE", "")
    with TestClient(app) as client:
        assert "Sign-up is closed" in client.get("/signup").text
        assert "/signup" not in client.get("/login").text
        assert _signup(client, code="").status_code == 403
    assert conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 1  # just the fixture's


def test_signup_attempts_are_rate_limited_per_ip(db):
    with TestClient(app) as client:
        for i in range(10):
            _signup(client, username=f"bot{i}", code="guess")
        resp = _signup(client)  # even a correct one, once the IP is over the limit
    assert resp.status_code == 429


# --- what the database knows ---


def test_usernames_are_never_stored(conn):
    with TestClient(app) as client:
        assert _signup(client, username="Very-Distinct-Name").status_code == 303
        login(client, username="very-distinct-name", password="a fine password")
        client.get("/")
    dump = " ".join(str(v) for t in ("users", "sessions") for row in
                    conn.execute(f"SELECT * FROM {t}").fetchall() for v in row.values())
    assert "very-distinct-name" not in dump.lower()
    stored = conn.execute("SELECT username_hash FROM users ORDER BY id DESC LIMIT 1").fetchone()
    assert stored["username_hash"] == username_hash(bytes.fromhex(TEST_USERNAME_KEY), "very-distinct-name")
    assert stored["username_hash"] != username_hash(bytes(32), "very-distinct-name")  # keyed
    assert stored["username_hash"] != hashlib.sha256(b"very-distinct-name").hexdigest()  # not a plain hash


def test_the_app_refuses_to_start_without_a_strong_username_key(db, monkeypatch):
    for bad in ("", "not-hex", "ab" * 16):  # missing, garbage, only 16 bytes
        monkeypatch.setenv("USERNAME_KEY", bad)
        with pytest.raises(RuntimeError, match="USERNAME_KEY"), TestClient(app):
            pass


def test_no_page_ever_shows_a_username(db):
    with TestClient(app) as client:
        login(client)
        for path in ("/", "/more", "/account", "/profile", "/me", "/me/all.json"):
            assert TEST_USERNAME not in client.get(path).text, path
