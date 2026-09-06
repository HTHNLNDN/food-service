from decimal import Decimal

from app.agent import Recipe
from app.db import bootstrap, connect
from app.nutrition import Ingredient, Macros
from app.offers import Offer
from app.planner import decline, generate_plan, load_plan
from app.profile import Preferences, save_preferences
from app.shopping import build_shopping_list

WITHIN = Macros(100, 20, 0, 0)


class FakeAgent:
    def __init__(self, initial, replacement):
        self.initial = initial
        self.replacement = replacement
        self.replace_calls = 0

    def plan_week(self, ctx):
        return list(self.initial)

    def replace(self, declined, ctx):
        self.replace_calls += 1
        return self.replacement


class FakeOffers:
    def __init__(self, offers=()):
        self._o = list(offers)

    def offers(self, selected):
        return list(self._o)


class FakeNutrition:
    def __init__(self, per100=WITHIN):
        self.per100 = per100

    def lookup(self, name):
        return (1, self.per100)


def _recipe(title, *ings):
    # 400 g/ingredient over 4 servings -> 20 g protein/serving with WITHIN, i.e. on-target
    return Recipe(title, 4, [Ingredient(n, 400) for n in ings], ["cook"])


def _conn(tmp_path):
    conn = connect(tmp_path / "d.db")
    bootstrap(conn)
    save_preferences(conn, Preferences(600, 15, "", 2, ["rema"]))
    return conn


def _plan(conn, agent):
    return generate_plan(conn, agent=agent, offers=FakeOffers(), nutrition=FakeNutrition(), num_dinners=2)


def test_decline_replaces_dish_and_persists(tmp_path):
    conn = _conn(tmp_path)
    agent = FakeAgent([_recipe("A", "unicorn"), _recipe("B", "beef")], _recipe("C", "chicken"))
    plan = _plan(conn, agent)

    updated = decline(conn, plan.id, 0, agent=agent, offers=FakeOffers(), nutrition=FakeNutrition())
    assert [r.title for r in updated.recipes] == ["C", "B"]
    assert not updated.recipes[0].flagged
    # persisted
    assert [r.title for r in load_plan(conn, plan.id).recipes] == ["C", "B"]


def test_declined_unique_ingredient_removed_from_shopping(tmp_path):
    conn = _conn(tmp_path)
    agent = FakeAgent([_recipe("A", "unicorn"), _recipe("B", "beef")], _recipe("C", "chicken"))
    plan = _plan(conn, agent)
    updated = decline(conn, plan.id, 0, agent=agent, offers=FakeOffers(), nutrition=FakeNutrition())

    offers = [Offer("rema", "Chicken", Decimal(20), "DKK", "", "", None, None, None, "campaign")]
    sl = build_shopping_list(updated.recipes, offers, ["rema"])
    all_names = [i.ingredient for i in sl.items] + [u.ingredient for u in sl.unmatched]
    assert "unicorn" not in all_names  # unique ingredient of the declined dish is gone
    assert "chicken" in all_names       # replacement's ingredient is present
