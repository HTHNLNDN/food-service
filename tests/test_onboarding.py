"""Onboarding: state, routes, sign-up, the plan-page card, export, stats."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import onboarding
from app.bans import BEST_EFFORT_NOTICE
from app.main import app
from app.profile import export_all, load_preferences
from tests.conftest import TEST_SIGNUP_CODE, TEST_USER_ID, add_user, login


def test_start_saves_defaults_and_a_welcome_row(conn):
    onboarding.start(conn, TEST_USER_ID)
    prefs = load_preferences(conn, TEST_USER_ID)
    assert (prefs.servings, prefs.max_kcal, prefs.min_protein_g) == (2, 700, 25)
    assert prefs.bans == [] and prefs.stores == []
    ob = onboarding.load(conn, TEST_USER_ID)
    assert ob.step == "welcome" and ob.active
    assert ob.completed_at is None and ob.skipped_at is None and ob.dismissed_at is None


def test_accounts_without_a_row_have_no_onboarding(conn):
    assert onboarding.load(conn, TEST_USER_ID) is None
    assert onboarding.card_steps_left(conn, TEST_USER_ID) is None


def test_next_step_passes_over_steps_not_built_yet():
    assert onboarding.next_step("welcome") == "household"
    assert onboarding.next_step("household") == "goals"
    assert onboarding.next_step("goals") == "leave_out"
    assert onboarding.next_step("leave_out") == "cooking"
    assert onboarding.next_step("cooking") == "review"  # the stores step isn't built yet
    assert onboarding.next_step("review") == "review"


def test_advance_moves_the_resume_point(conn):
    onboarding.start(conn, TEST_USER_ID)
    assert onboarding.advance(conn, TEST_USER_ID, "welcome") == "household"
    assert onboarding.load(conn, TEST_USER_ID).step == "household"


def test_steps_left_counts_built_screens_from_the_resume_point(conn):
    onboarding.start(conn, TEST_USER_ID)
    assert onboarding.card_steps_left(conn, TEST_USER_ID) == 5   # household … cooking, review
    onboarding.advance(conn, TEST_USER_ID, "welcome")
    assert onboarding.card_steps_left(conn, TEST_USER_ID) == 5
    onboarding.advance(conn, TEST_USER_ID, "household")
    assert onboarding.card_steps_left(conn, TEST_USER_ID) == 4   # goals, leave_out, cooking, review


def test_skip_complete_and_dismiss_end_the_card(conn):
    for end in (onboarding.skip_all, onboarding.complete, onboarding.dismiss):
        conn.execute("DELETE FROM user_onboarding")
        onboarding.start(conn, TEST_USER_ID)
        end(conn, TEST_USER_ID)
        assert onboarding.card_steps_left(conn, TEST_USER_ID) is None


def test_skip_and_complete_make_onboarding_inactive_but_dismiss_does_not(conn):
    onboarding.start(conn, TEST_USER_ID)
    onboarding.dismiss(conn, TEST_USER_ID)
    assert onboarding.load(conn, TEST_USER_ID).active      # can still finish from /onboarding
    onboarding.skip_all(conn, TEST_USER_ID)
    assert not onboarding.load(conn, TEST_USER_ID).active


# --- sign-up starts onboarding ---


def _signup(client, username="newbie"):
    return client.post("/signup", data={
        "invite_code": TEST_SIGNUP_CODE, "username": username, "password": "a fine password",
        "confirm_password": "a fine password", "understood": "1"}, follow_redirects=False)


def _new_user_id(conn, before: set[int]) -> int:
    ids = {r["id"] for r in conn.execute("SELECT id FROM users")}
    (new,) = ids - before
    return new


def test_signup_starts_onboarding_with_defaults(conn):
    before = {r["id"] for r in conn.execute("SELECT id FROM users")}
    with TestClient(app) as client:
        resp = _signup(client)
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding"
    user_id = _new_user_id(conn, before)
    assert onboarding.load(conn, user_id).step == "welcome"
    assert load_preferences(conn, user_id).max_kcal == 700


def test_a_failed_signup_leaves_no_onboarding_row(conn):
    with TestClient(app) as client:
        _signup(client)
        again = _signup(client)  # same username: refused
    assert again.status_code == 400
    assert conn.execute("SELECT COUNT(*) n FROM user_onboarding").fetchone()["n"] == 1


def test_existing_accounts_are_never_sent_to_onboarding(db):
    with TestClient(app) as client:
        login(client)  # the test user predates onboarding: no row
        home = client.get("/", follow_redirects=False)
        setup = client.get("/onboarding", follow_redirects=False)
    assert home.status_code == 200 and "Finish setting up" not in home.text
    assert setup.status_code == 303 and setup.headers["location"] == "/"


# --- the screens ---


@pytest.fixture
def new_client(conn):
    """A signed-up brand-new account, mid-onboarding, plus its user id."""
    before = {r["id"] for r in conn.execute("SELECT id FROM users")}
    with TestClient(app) as client:
        _signup(client)
        yield client, _new_user_id(conn, before)


def test_onboarding_resumes_where_you_left_off(new_client):
    client, _ = new_client
    assert client.get("/onboarding", follow_redirects=False).headers["location"] == \
        "/onboarding/welcome"
    client.post("/onboarding/welcome", data={"action": "next"})
    assert client.get("/onboarding", follow_redirects=False).headers["location"] == \
        "/onboarding/household"


def test_welcome_frames_the_journey_and_offers_skip_setup(new_client):
    client, _ = new_client
    page = client.get("/onboarding/welcome").text
    assert "lifelong journey" in page and "Let's go" in page and "Skip setup" in page


def test_household_asks_why_and_counts_as_step_1_of_5(new_client):
    client, _ = new_client
    page = client.get("/onboarding/household").text
    assert "How many people do you cook dinner for?" in page
    assert "Step 1 of 5" in page
    assert "<details" in page and "Why?" in page
    assert 'value="2" checked' in page  # the default is pre-selected
    assert "Skip setup" in page and ">Skip<" in page


def test_household_answer_sets_servings_and_moves_on(new_client, conn):
    client, user_id = new_client
    resp = client.post("/onboarding/household", data={"action": "next", "servings": "4"},
                       follow_redirects=False)
    assert resp.headers["location"] == "/onboarding/goals"
    assert load_preferences(conn, user_id).servings == 4


@pytest.mark.parametrize("bad", ["0", "9", "-1", "two", ""])
def test_household_refuses_out_of_range_servings(new_client, conn, bad):
    client, user_id = new_client
    resp = client.post("/onboarding/household", data={"action": "next", "servings": bad})
    assert resp.status_code == 422 and "between 1 and 8" in resp.text
    assert load_preferences(conn, user_id).servings == 2
    assert onboarding.load(conn, user_id).step == "welcome"


def test_skip_keeps_the_default_and_moves_on(new_client, conn):
    client, user_id = new_client
    resp = client.post("/onboarding/household", data={"action": "skip", "servings": "6"},
                       follow_redirects=False)
    assert resp.headers["location"] == "/onboarding/goals"
    assert load_preferences(conn, user_id).servings == 2


def test_steps_not_built_yet_pass_straight_to_the_next(new_client):
    client, _ = new_client
    for step, to in (("stores", "review"),):
        resp = client.get(f"/onboarding/{step}", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == f"/onboarding/{to}"


def test_unknown_step_is_404(new_client):
    client, _ = new_client
    assert client.get("/onboarding/nope").status_code == 404
    assert client.post("/onboarding/nope", data={"action": "next"}).status_code == 404


def test_review_shows_what_was_set_and_finishes(new_client, conn):
    client, user_id = new_client
    client.post("/onboarding/household", data={"action": "next", "servings": "3"})
    page = client.get("/onboarding/review").text
    assert "Here's what I set up and why" in page
    assert "3 people" in page and "700 kcal" in page and "25 g protein" in page
    assert 'href="/onboarding/household"' in page  # edit link
    resp = client.post("/onboarding/review", data={"action": "done"}, follow_redirects=False)
    assert resp.headers["location"] == "/"
    assert onboarding.load(conn, user_id).completed_at is not None


@pytest.mark.parametrize("step", ["welcome", "household", "review"])
def test_skip_setup_works_from_every_screen(new_client, conn, step):
    client, user_id = new_client
    resp = client.post(f"/onboarding/{step}", data={"action": "skip_all"},
                       follow_redirects=False)
    assert resp.headers["location"] == "/"
    ob = onboarding.load(conn, user_id)
    assert ob.skipped_at is not None and ob.completed_at is None
    prefs = load_preferences(conn, user_id)
    assert (prefs.servings, prefs.max_kcal, prefs.min_protein_g, prefs.bans) == (2, 700, 25, [])


def test_finished_or_skipped_onboarding_sends_you_home(new_client):
    client, _ = new_client
    client.post("/onboarding/welcome", data={"action": "skip_all"})
    for path in ("/onboarding", "/onboarding/household"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/"
    resp = client.post("/onboarding/household", data={"action": "next", "servings": "5"},
                       follow_redirects=False)
    assert resp.headers["location"] == "/"  # nothing changes after the fact


# --- the "Finish setting up" card ---


def test_card_shows_steps_left_and_links_to_resume(new_client):
    client, _ = new_client
    page = client.get("/").text
    assert "Finish setting up (5 steps left)" in page and 'href="/onboarding"' in page
    client.post("/onboarding/welcome", data={"action": "next"})
    client.post("/onboarding/household", data={"action": "skip"})
    assert "Finish setting up (4 steps left)" in client.get("/").text


def test_card_can_be_dismissed(new_client):
    client, _ = new_client
    resp = client.post("/onboarding/dismiss", follow_redirects=False)
    assert resp.headers["location"] == "/"
    assert "Finish setting up" not in client.get("/").text


def test_card_is_gone_once_done_or_skipped(new_client):
    client, _ = new_client
    client.post("/onboarding/review", data={"action": "done"})
    assert "Finish setting up" not in client.get("/").text


# --- export and stats ---


def test_export_includes_the_onboarding_row(conn):
    assert export_all(conn, TEST_USER_ID)["onboarding"] is None
    onboarding.start(conn, TEST_USER_ID)
    onboarding.complete(conn, TEST_USER_ID)
    data = export_all(conn, TEST_USER_ID)["onboarding"]
    assert data["step"] == "welcome" and data["completed_at"] is not None
    assert data["skipped_at"] is None and data["goals"] == [] and data["cooking_style"] == ""
    datetime.fromisoformat(data["started_at"])  # serialisable


def _row(conn, user_id, started, completed=None, skipped=None):
    conn.execute("INSERT INTO user_onboarding (user_id, started_at, completed_at, skipped_at, "
                 "step) VALUES (%s, %s, %s, %s, 'review')", (user_id, started, completed, skipped))


def test_stats_count_each_outcome_and_the_median(conn):
    from scripts.onboarding_stats import stats

    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    users = [TEST_USER_ID] + [add_user(conn, f"user{i}") for i in range(4)]
    _row(conn, users[0], now - timedelta(minutes=10), completed=now - timedelta(minutes=8))
    _row(conn, users[1], now - timedelta(minutes=10), completed=now - timedelta(minutes=6))
    _row(conn, users[2], now - timedelta(days=1), skipped=now - timedelta(days=1))
    _row(conn, users[3], now - timedelta(days=2))   # in progress
    _row(conn, users[4], now - timedelta(days=9))   # abandoned (> 7 days)
    assert stats(conn, now) == {"started": 5, "completed": 2, "skipped": 1, "in_progress": 1,
                                "abandoned": 1, "median_minutes": 3.0}


def test_stats_with_no_rows(conn):
    from scripts.onboarding_stats import stats

    assert stats(conn, datetime.now(UTC))["median_minutes"] is None


# --- foods to leave out ---


def test_leave_out_shows_three_groups_the_notice_and_the_hint(new_client):
    client, _ = new_client
    page = client.get("/onboarding/leave_out").text
    for group in ("Diets", "Allergies &amp; intolerances", "Just not a fan"):
        assert group in page
    assert 'value="vegan"' in page and 'value="nuts"' in page and 'value="blue cheese"' in page
    assert BEST_EFFORT_NOTICE.split(". ")[-1] in page  # "If you have a serious allergy, …"
    assert "leave out names and medical details" in page
    assert "Step 3 of 5" in page


def test_leave_out_saves_chips_and_custom_items_as_bans(new_client, conn):
    client, user_id = new_client
    resp = client.post("/onboarding/leave_out", data={
        "action": "next", "bans": ["fish", "mushrooms"], "bans_custom": "Kale, kale, okra"},
        follow_redirects=False)
    assert resp.headers["location"] == "/onboarding/cooking"
    assert load_preferences(conn, user_id).bans == ["fish", "mushrooms", "kale", "okra"]


def test_leave_out_prefills_and_keeps_existing_custom_bans(new_client):
    client, _ = new_client
    client.post("/onboarding/leave_out", data={"action": "next", "bans": ["egg"],
                                               "bans_custom": "okra"})
    page = client.get("/onboarding/leave_out").text
    assert 'value="egg" checked' in page and 'value="okra" checked' in page


def test_leave_out_skip_keeps_bans(new_client, conn):
    client, user_id = new_client
    client.post("/onboarding/leave_out", data={"action": "skip", "bans": ["fish"]})
    assert load_preferences(conn, user_id).bans == []


def test_review_lists_foods_left_out_by_name(new_client):
    client, _ = new_client
    client.post("/onboarding/leave_out", data={"action": "next", "bans": ["dairy", "blue cheese"]})
    page = client.get("/onboarding/review").text
    assert "Dairy &amp; lactose, Blue cheese" in page
    assert 'href="/onboarding/leave_out"' in page


# --- how you like to cook ---


def test_cooking_step_saves_chips_and_free_text(new_client, conn):
    client, user_id = new_client
    page = client.get("/onboarding/cooking").text
    assert "Step 4 of 5" in page and "leave out names and medical details" in page
    resp = client.post("/onboarding/cooking", data={
        "action": "next", "cooking": ["No deep-frying", "Short ingredient lists"],
        "cooking_note": "  air fryer when possible "}, follow_redirects=False)
    assert resp.headers["location"] == "/onboarding/review"
    assert onboarding.answers(conn, user_id).cooking_style == \
        "No deep-frying; Short ingredient lists; air fryer when possible"
    page = client.get("/onboarding/cooking").text
    assert 'value="No deep-frying" checked' in page and "air fryer when possible" in page
    assert "air fryer when possible" in client.get("/onboarding/review").text


def test_cooking_skip_leaves_it_empty(new_client, conn):
    client, user_id = new_client
    client.post("/onboarding/cooking", data={"action": "skip", "cooking": ["No seed oils"]})
    assert onboarding.answers(conn, user_id).cooking_style == ""


def test_answers_are_empty_without_onboarding(conn):
    assert onboarding.answers(conn, TEST_USER_ID) == onboarding.Answers([], [], "")


# --- the notice next to plans ---


def test_plan_page_shows_the_notice_only_when_something_is_left_out(new_client):
    from app.agent import Recipe
    from app.nutrition import Ingredient, Macros
    from app.services import Services

    class Agent:
        def plan_week(self, ctx):
            return [Recipe("Pork stew", 2, [Ingredient("pork", 400)], ["Cook."])]

        def replace(self, declined, ctx):
            return declined

    class Nutrition:
        def lookup(self, name):
            return (1, Macros(100, 30, 0, 0))

    class Offers:
        def offers(self, selected):
            return []

    client, _ = new_client
    client.app.state.build_services = lambda c, conn: Services(Agent(), Offers(), Nutrition(), None)
    client.app.state.plan_sync = True
    client.post("/plan")
    notice = "If you have a serious allergy, always check the ingredients yourself."
    assert "Pork stew" in client.get("/").text and notice not in client.get("/").text
    client.post("/onboarding/leave_out", data={"action": "next", "bans": ["fish"]})
    assert notice in client.get("/").text


# --- goals, targets and conditions ---


def test_goals_step_lists_5_goals_the_condition_and_no_numbers(new_client):
    client, _ = new_client
    page = client.get("/onboarding/goals").text
    assert "Step 2 of 5" in page and "Pick up to 3" in page
    for label in ("Eat a bit healthier", "Lose weight, gently", "More protein", "Save money",
                  "Eat more plants"):
        assert label in page
    assert "PMOS (formerly PCOS)" in page
    assert "Adds: lower-GI carbohydrates, good protein and fibre, unsaturated fats" in page
    assert "Not sure? I'll start you on balanced dinners." in page
    assert "kcal" not in page and "weight?" not in page.lower()  # not numbers-first


def test_goals_set_the_targets_and_are_remembered(new_client, conn):
    client, user_id = new_client
    resp = client.post("/onboarding/goals", data={
        "action": "next", "goals": ["lose_gently", "more_protein"], "conditions": ["pmos"]},
        follow_redirects=False)
    assert resp.headers["location"] == "/onboarding/leave_out"
    prefs = load_preferences(conn, user_id)
    assert (prefs.max_kcal, prefs.min_protein_g) == (550, 40)
    saved = onboarding.answers(conn, user_id)
    assert (saved.goals, saved.conditions) == (["lose_gently", "more_protein"], ["pmos"])
    page = client.get("/onboarding/goals").text
    assert 'value="lose_gently" checked' in page and 'value="pmos" checked' in page


def test_more_than_3_goals_is_refused(new_client, conn):
    client, user_id = new_client
    resp = client.post("/onboarding/goals", data={
        "action": "next", "goals": ["healthier", "lose_gently", "more_protein", "save_money"]})
    assert resp.status_code == 422 and "up to 3" in resp.text
    assert onboarding.answers(conn, user_id).goals == []
    assert load_preferences(conn, user_id).max_kcal == 700


def test_unknown_goal_and_condition_ids_are_ignored(new_client, conn):
    client, user_id = new_client
    client.post("/onboarding/goals", data={"action": "next", "goals": ["nope", "save_money"],
                                           "conditions": ["diabetes"]})
    saved = onboarding.answers(conn, user_id)
    assert (saved.goals, saved.conditions) == (["save_money"], [])


def test_skipping_goals_gives_balanced_700_25(new_client, conn):
    client, user_id = new_client
    client.post("/onboarding/goals", data={"action": "next", "goals": ["lose_gently"],
                                           "conditions": ["pmos"]})
    client.post("/onboarding/goals", data={"action": "skip", "goals": ["more_protein"]})
    prefs = load_preferences(conn, user_id)
    assert (prefs.max_kcal, prefs.min_protein_g) == (700, 25)
    assert onboarding.answers(conn, user_id) == onboarding.Answers([], [], "")


def test_review_shows_goals_targets_condition_and_the_medical_notice(new_client):
    client, _ = new_client
    client.post("/onboarding/goals", data={"action": "next", "goals": ["more_protein"],
                                           "conditions": ["pmos"]})
    page = client.get("/onboarding/review").text
    assert "More protein" in page and 'href="/onboarding/goals"' in page
    assert "around 750 kcal with at least 40 g protein" in page
    assert "standard portion" in page
    assert "PMOS (formerly PCOS)" in page and "lower-GI carbohydrates" in page
    assert "I plan everyday dinners, not medical diets." in page


def test_review_without_goals_says_balanced(new_client):
    client, _ = new_client
    assert "Balanced dinners" in client.get("/onboarding/review").text


def test_condition_pointers_reach_the_ai_but_never_its_name(new_client, conn):
    from app.agent import _SYSTEM, _plan_prompt
    from app.planner import generate_plan
    from tests.test_planner import WITHIN, FakeAgent, FakeNutrition, FakeOffers, _recipe

    client, user_id = new_client
    client.post("/onboarding/goals", data={"action": "next", "goals": ["save_money"],
                                           "conditions": ["pmos"]})
    agent = FakeAgent([_recipe("A")], _recipe("R"))
    generate_plan(conn, user_id, agent=agent, offers=FakeOffers([]),
                  nutrition=FakeNutrition(WITHIN), num_dinners=1)
    prompt = _SYSTEM + _plan_prompt(agent.last_ctx)
    assert "lower-GI carbohydrates" in prompt and "offers" in prompt
    assert "pmos" not in prompt.lower() and "pcos" not in prompt.lower()
