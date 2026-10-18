from app.agent import Recipe
from app.nutrition import Ingredient, Macros
from app.planner import generate_plan
from app.profile import (
    Preferences,
    preference_summary,
    rating_history,
    save_preferences,
    save_rating,
)
from tests.conftest import TEST_USER_ID


def test_rating_persists_and_shows_in_history(conn):
    save_rating(conn, TEST_USER_ID, "Chicken bowl", 1, ["chicken breast", "broccoli"])
    hist = rating_history(conn, TEST_USER_ID)
    assert len(hist) == 1
    assert hist[0].title == "Chicken bowl"
    assert hist[0].score == 1
    assert "chicken breast" in hist[0].tags


def test_preference_summary_empty_then_shifts_with_ratings(conn):
    assert preference_summary(conn, TEST_USER_ID) == ""
    save_rating(conn, TEST_USER_ID, "Chicken bowl", 1, ["chicken breast"])
    save_rating(conn, TEST_USER_ID, "Liver mush", -1, ["liver"])
    summary = preference_summary(conn, TEST_USER_ID)
    assert "chicken breast" in summary  # liked
    assert "liver" in summary           # disliked


def test_summary_feeds_next_plan_context(conn):
    save_preferences(conn, TEST_USER_ID, Preferences(600, 5, "", 2, ["rema"]))
    save_rating(conn, TEST_USER_ID, "X", 1, ["chicken breast"])

    captured = {}

    class CapAgent:
        def plan_week(self, ctx):
            captured["ctx"] = ctx
            return [Recipe("A", 4, [Ingredient("chicken breast", 400)], ["c"])]

        def replace(self, declined, ctx):
            return Recipe("R", 4, [Ingredient("chicken breast", 400)], ["c"])

    class FakeOffers:
        def offers(self, selected):
            return []

    class FakeNutrition:
        def lookup(self, name):
            return (1, Macros(100, 20, 0, 0))

    generate_plan(conn, TEST_USER_ID, agent=CapAgent(), offers=FakeOffers(), nutrition=FakeNutrition(), num_dinners=1)
    assert "chicken breast" in captured["ctx"].preference_summary
