from app.agent import Recipe
from app.db import bootstrap, connect
from app.nutrition import Ingredient, Macros
from app.planner import generate_plan
from app.profile import (
    Preferences,
    preference_summary,
    rating_history,
    save_preferences,
    save_rating,
)


def _conn(tmp_path):
    conn = connect(tmp_path / "f.db")
    bootstrap(conn)
    return conn


def test_rating_persists_and_shows_in_history(tmp_path):
    conn = _conn(tmp_path)
    save_rating(conn, "Chicken bowl", 1, ["chicken breast", "broccoli"])
    hist = rating_history(conn)
    assert len(hist) == 1
    assert hist[0].title == "Chicken bowl"
    assert hist[0].score == 1
    assert "chicken breast" in hist[0].tags


def test_preference_summary_empty_then_shifts_with_ratings(tmp_path):
    conn = _conn(tmp_path)
    assert preference_summary(conn) == ""
    save_rating(conn, "Chicken bowl", 1, ["chicken breast"])
    save_rating(conn, "Liver mush", -1, ["liver"])
    summary = preference_summary(conn)
    assert "chicken breast" in summary  # liked
    assert "liver" in summary           # disliked


def test_summary_feeds_next_plan_context(tmp_path):
    conn = _conn(tmp_path)
    save_preferences(conn, Preferences(600, 5, "", 2, ["rema"]))
    save_rating(conn, "X", 1, ["chicken breast"])

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

    generate_plan(conn, agent=CapAgent(), offers=FakeOffers(), nutrition=FakeNutrition(), num_dinners=1)
    assert "chicken breast" in captured["ctx"].preference_summary
