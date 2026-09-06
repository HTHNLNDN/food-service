from datetime import UTC, datetime

from app.db import bootstrap, connect
from app.history import (
    PastDish,
    _norm_title,
    is_repeat,
    recent_avoided,
)
from app.nutrition import Ingredient
from app.profile import save_rating


def _conn(tmp_path):
    conn = connect(tmp_path / "h.db")
    bootstrap(conn)
    return conn


def _add_plan(conn, dishes):
    """dishes: list of (title, [ingredient names]). Persists one plan; returns its id."""
    cur = conn.execute(
        "INSERT INTO plans (created_at, week) VALUES (?, ?)",
        (datetime.now(UTC).isoformat(), "2026-W01"),
    )
    plan_id = cur.lastrowid
    for slot, (title, names) in enumerate(dishes):
        rc = conn.execute(
            "INSERT INTO recipes (plan_id, slot_index, title, servings, steps) VALUES (?, ?, ?, ?, '[]')",
            (plan_id, slot, title, 4),
        )
        conn.executemany(
            "INSERT INTO recipe_ingredients (recipe_id, name, grams) VALUES (?, ?, 100)",
            [(rc.lastrowid, n) for n in names],
        )
    conn.commit()
    return plan_id


def _ings(*names):
    return [Ingredient(n, 100) for n in names]


# --- normalization + matching ---


def test_norm_title_merges_ampersand_and_punctuation():
    assert _norm_title("Creamy Garlic Chicken & Broccoli Skillet") == \
           _norm_title("Creamy Garlic Chicken and Broccoli Skillet")


def test_is_repeat_by_title_ignores_ingredients():
    past = [PastDish.of("Thai Basil Chicken Stir-Fry", ["chicken", "basil"])]
    # same dish name, totally different ingredients listed -> still a repeat
    assert is_repeat("thai basil chicken stir fry", _ings("tofu"), past)


def test_is_repeat_by_ingredient_similarity():
    past = [PastDish.of("Dinner A", ["chicken breast", "broccoli", "garlic", "rapeseed oil", "cottage cheese"])]
    # 4 of 5 shared -> Jaccard 0.8 >= 0.6, different title
    assert is_repeat("Totally New Name", _ings("chicken breast", "broccoli", "garlic", "rapeseed oil"), past)


def test_not_a_repeat_when_distinct():
    past = [PastDish.of("Dinner A", ["chicken breast", "broccoli"])]
    assert not is_repeat("Beef Chili", _ings("beef", "kidney beans", "tomato"), past)


# --- cooldown selection ---


def test_recent_neutral_dish_is_avoided(tmp_path):
    conn = _conn(tmp_path)
    _add_plan(conn, [("Chicken Bowl", ["chicken breast", "rice"])])
    titles = {p.title for p in recent_avoided(conn)}
    assert "Chicken Bowl" in titles


def test_liked_dish_is_not_avoided(tmp_path):
    conn = _conn(tmp_path)
    _add_plan(conn, [("Loved Dish", ["salmon"]), ("Meh Dish", ["pork"])])
    save_rating(conn, "Loved Dish", 1, ["salmon"])  # 👍
    titles = {p.title for p in recent_avoided(conn)}
    assert "Loved Dish" not in titles   # liked -> may return
    assert "Meh Dish" in titles         # neutral -> still on cooldown


def test_disliked_dish_avoided_even_beyond_window(tmp_path):
    conn = _conn(tmp_path)
    _add_plan(conn, [("Hated Dish", ["liver"])])       # oldest plan
    for _ in range(5):                                  # push it well past the 4-plan window
        _add_plan(conn, [("Filler", ["chicken"])])
    save_rating(conn, "Hated Dish", -1, ["liver"])     # 👎
    titles = {p.title for p in recent_avoided(conn, window=4)}
    assert "Hated Dish" in titles                       # disliked -> permanent avoid


def test_neutral_dish_falls_off_after_window(tmp_path):
    conn = _conn(tmp_path)
    _add_plan(conn, [("Old Neutral", ["cod"])])         # oldest
    for _ in range(4):
        _add_plan(conn, [("Filler", ["chicken"])])
    titles = {p.title for p in recent_avoided(conn, window=4)}
    assert "Old Neutral" not in titles                  # aged out of the cooldown window
