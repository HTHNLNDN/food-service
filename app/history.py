"""Anti-repetition: a recency cooldown with a like-exemption (temporal diversity).

Grounded in recommender-systems "temporal diversity" (Lathia et al., SIGIR 2010) and the
explore/exploit view of food recommendation: cool down recently-served dinners so the plan
doesn't feel monotonous (sensory-specific satiety builds over weeks), but exempt the ones the
user LIKED — those they want back. Rules, by the user's rating of a dish (by title):

  liked  (net 👍) -> never avoided; free to return
  disliked (net 👎) -> always avoided (permanent), even beyond the window
  neutral (unrated) -> avoided while it sits in the last RECENT_PLANS plans, then allowed again

Enforced like bans/macros: the titles are fed to the agent as a soft "don't repeat" hint, and
`is_repeat` is a deterministic guard in the planner that revises a repeat out. A candidate
counts as a repeat if its normalized title matches, or its ingredient set is >= SIMILARITY
similar (Jaccard) to a cooled-down dish — which catches reworded near-duplicates.
"""

import re
import sqlite3
from dataclasses import dataclass

from app.nutrition import Ingredient

RECENT_PLANS = 4        # cooldown window ~= 4 weeks (one plan per week)
SIMILARITY = 0.6        # ingredient-set Jaccard at/above which two dinners are "the same"
AVOID_PROMPT_CAP = 25   # how many recent titles to list in the prompt

_MODIFIERS = frozenset({
    "fresh", "dried", "frozen", "canned", "cooked", "raw", "boneless", "skinless", "chopped",
    "diced", "minced", "sliced", "ground", "large", "small", "medium", "whole", "ripe", "extra",
    "virgin", "fine", "light", "plain", "smoked", "lean", "baby", "of", "and", "or",
})


def _norm_title(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.lower().replace("&", " and ")))


def _norm_ing(name: str) -> str:
    toks = [t for t in re.findall(r"[a-z]+", name.lower()) if t not in _MODIFIERS]
    return " ".join(toks) or name.strip().lower()


def _norm_ings(names) -> frozenset[str]:
    return frozenset(_norm_ing(n) for n in names)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class PastDish:
    title: str                    # original title, for the prompt hint
    norm_title: str
    ingredients: frozenset[str]   # normalized ingredient names

    @classmethod
    def of(cls, title: str, ingredient_names) -> "PastDish":
        return cls(title, _norm_title(title), _norm_ings(ingredient_names))


def is_repeat(title: str, ingredients: list[Ingredient], past: list[PastDish],
              *, threshold: float = SIMILARITY) -> bool:
    """True if this dish repeats one in `past` — same normalized title, or ingredients that
    overlap it by >= threshold (Jaccard)."""
    nt = _norm_title(title)
    ci = _norm_ings(i.name for i in ingredients)
    for p in past:
        if nt and p.norm_title == nt:
            return True
        if _jaccard(ci, p.ingredients) >= threshold:
            return True
    return False


def _title_net_scores(conn: sqlite3.Connection) -> dict[str, int]:
    net: dict[str, int] = {}
    for row in conn.execute("SELECT title, score FROM ratings"):
        nt = _norm_title(row["title"])
        net[nt] = net.get(nt, 0) + row["score"]
    return net


def _load_ingredients(conn: sqlite3.Connection, recipe_id: int) -> list[str]:
    return [r["name"] for r in conn.execute(
        "SELECT name FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))]


def recent_avoided(conn: sqlite3.Connection, *, window: int = RECENT_PLANS) -> list[PastDish]:
    """Dishes to keep off the next plan: recent (non-liked) dinners + all disliked dinners.

    Liked dishes are excluded entirely so they can come back. De-duplicated by title (a dish
    served several weeks contributes one entry, with its ingredients unioned)."""
    net = _title_net_scores(conn)
    liked = {t for t, s in net.items() if s > 0}
    disliked = {t for t, s in net.items() if s < 0}

    dishes: dict[str, PastDish] = {}

    def add(recipe_id: int, title: str) -> None:
        nt = _norm_title(title)
        ings = _norm_ings(_load_ingredients(conn, recipe_id))
        existing = dishes.get(nt)
        merged = (existing.ingredients | ings) if existing else ings
        dishes[nt] = PastDish(title, nt, merged)

    recent_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM plans ORDER BY id DESC LIMIT ?", (window,))]
    if recent_ids:
        placeholders = ",".join("?" * len(recent_ids))
        for r in conn.execute(
            f"SELECT id, title FROM recipes WHERE plan_id IN ({placeholders})", recent_ids
        ):
            if _norm_title(r["title"]) not in liked:  # neutral or disliked; liked can return
                add(r["id"], r["title"])

    if disliked:  # disliked dishes stay avoided forever, not just within the window
        for r in conn.execute("SELECT id, title FROM recipes"):
            if _norm_title(r["title"]) in disliked:
                add(r["id"], r["title"])

    return list(dishes.values())
