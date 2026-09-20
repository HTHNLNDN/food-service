from app.agent import PlanContext, Recipe
from app.db import bootstrap, connect
from app.nutrition import Ingredient, Macros
from app.planner import (
    PlannedRecipe,
    decline,
    generate_plan,
    is_print_compact,
    load_latest_plan,
    weekday_meals,
)
from app.profile import Preferences, save_preferences, save_rating
from app.translate import Translated

# --- fakes ---


class FakeAgent:
    def __init__(self, recipes, replacement):
        self._recipes = recipes
        self._replacement = replacement
        self.replace_calls = 0
        self.last_ctx = None

    def plan_week(self, ctx: PlanContext):
        self.last_ctx = ctx
        return list(self._recipes)

    def replace(self, declined, ctx):
        self.replace_calls += 1
        self.last_ctx = ctx
        return self._replacement


class FakeOffer:
    def __init__(self, name):
        self.name = name


class FakeOffers:
    def __init__(self, offers):
        self._offers = offers

    def offers(self, selected):
        return list(self._offers)


class FakeNutrition:
    """Every ingredient resolves to the same per-100g macros (configurable)."""

    def __init__(self, per100):
        self.per100 = per100

    def lookup(self, name):
        return (1, self.per100)


def _recipe(title, grams=400, servings=4):
    return Recipe(
        title=title,
        servings=servings,
        ingredients=[Ingredient(name="stuff", grams=grams)],
        steps=["cook"],
    )


def _conn(tmp_path, *, servings=2, max_kcal=600, min_protein=15, stores=("rema",), bans=(), language=""):
    conn = connect(tmp_path / "p.db")
    bootstrap(conn)
    save_preferences(conn, Preferences(max_kcal, min_protein, "PCOS", servings, list(stores),
                                       list(bans), language))
    return conn


class FakeTranslator:
    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result = result
        self._raises = raises

    def translate(self, title, steps, ingredient_names, language):
        self.calls.append((title, steps, ingredient_names, language))
        if self._raises:
            raise self._raises
        return self._result


def _recipe_with(title, ingredient):
    return Recipe(title, 4, [Ingredient(ingredient, 400)], ["cook"])


def test_banned_ingredient_is_revised_out(tmp_path):
    conn = _conn(tmp_path, bans=["fish"])
    agent = FakeAgent([_recipe_with("Salmon Dinner", "salmon")], _recipe_with("Chicken Dinner", "chicken"))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN), num_dinners=1)
    assert plan.recipes[0].title == "Chicken Dinner"  # fish dish replaced
    assert not plan.recipes[0].flagged
    assert agent.replace_calls == 1


def test_persistently_banned_dish_is_flagged(tmp_path):
    conn = _conn(tmp_path, bans=["fish"])
    agent = FakeAgent([_recipe_with("Salmon A", "salmon")], _recipe_with("Salmon B", "salmon"))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN), num_dinners=1)
    assert plan.recipes[0].flagged
    assert agent.replace_calls <= 3  # bounded


# --- use-up ingredients ---


def test_use_up_is_persisted_and_passed_to_agent(tmp_path):
    conn = _conn(tmp_path)
    agent = FakeAgent([_recipe("A")], replacement=_recipe("R"))
    generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN),
                  num_dinners=1, use_up="3kg chicken breast")
    assert agent.last_ctx.use_up == "3kg chicken breast"       # reached the prompt context
    assert load_latest_plan(conn).use_up == "3kg chicken breast"  # persisted on the plan


def test_decline_reuses_stored_use_up(tmp_path):
    conn = _conn(tmp_path)
    agent = FakeAgent([_recipe_with("A", "chicken"), _recipe_with("B", "pork")], _recipe_with("R", "cod"))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN),
                         num_dinners=2, use_up="freezer veg")
    decline(conn, plan.id, 0, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN))
    assert agent.last_ctx.use_up == "freezer veg"  # replacement built with the stored use-up


# --- anti-repetition (recency cooldown with like-exemption) ---


def _seed_dish(conn, title, ingredient):
    """Persist a prior plan containing one dish, so it enters the cooldown history."""
    agent = FakeAgent([_recipe_with(title, ingredient)], _recipe_with("X", "x"))
    generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN), num_dinners=1)


def test_recent_dish_is_revised_out(tmp_path):
    conn = _conn(tmp_path)
    _seed_dish(conn, "Repeat Dish", "chicken breast")  # last week's dinner
    agent = FakeAgent([_recipe_with("Repeat Dish", "chicken breast")], _recipe_with("Fresh Dish", "cod"))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN), num_dinners=1)
    assert plan.recipes[0].title == "Fresh Dish"  # cooled-down repeat replaced
    assert agent.replace_calls == 1


def test_liked_dish_may_repeat(tmp_path):
    conn = _conn(tmp_path)
    _seed_dish(conn, "Loved Dish", "chicken breast")
    save_rating(conn, "Loved Dish", 1, ["chicken breast"])  # 👍 -> exempt from cooldown
    agent = FakeAgent([_recipe_with("Loved Dish", "chicken breast")], _recipe_with("Other", "cod"))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN), num_dinners=1)
    assert plan.recipes[0].title == "Loved Dish"  # liked dish allowed back, not revised
    assert agent.replace_calls == 0


def test_persistently_repeating_dish_is_flagged(tmp_path):
    conn = _conn(tmp_path)
    _seed_dish(conn, "Repeat Dish", "chicken breast")
    agent = FakeAgent([_recipe_with("Repeat Dish", "chicken breast")],
                      _recipe_with("Repeat Dish", "chicken breast"))  # replacement is the same repeat
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN), num_dinners=1)
    assert plan.recipes[0].flagged
    assert agent.replace_calls <= 3  # bounded


# per-100g profiles: within = 100 kcal/20 P; too-hot = 1000 kcal
# (a _recipe is 400 g over 4 servings -> per-serving macros equal the per-100g profile)
WITHIN = Macros(kcal=100, protein_g=20, carbs_g=0, fat_g=0)
TOO_HOT = Macros(kcal=1000, protein_g=20, carbs_g=0, fat_g=0)
NEAR = Macros(kcal=630, protein_g=39, carbs_g=0, fat_g=0)  # 30 kcal over, 1 g protein under


def test_near_miss_macros_are_accepted_within_slack(tmp_path):
    conn = _conn(tmp_path, max_kcal=600, min_protein=40)
    agent = FakeAgent([_recipe("A")], replacement=_recipe("R"))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(NEAR), num_dinners=1)
    assert not plan.recipes[0].flagged   # 630 kcal / 39 g protein tolerated
    assert agent.replace_calls == 0      # no slow revise for a near-miss


class _PartialNutrition:
    """Resolves everything to `per100` except the `missing` name(s), which return None."""

    def __init__(self, per100, missing):
        self.per100 = per100
        self.missing = {missing} if isinstance(missing, str) else set(missing)

    def lookup(self, name):
        return None if name in self.missing else (1, self.per100)


def test_tiny_unresolved_ingredient_is_tolerated(tmp_path):
    conn = _conn(tmp_path)
    dish = Recipe("Spiced Chicken", 4,
                  [Ingredient("chicken breast", 400), Ingredient("garam masala", 5)], ["cook"])
    agent = FakeAgent([dish], replacement=_recipe("R"))
    nutrition = _PartialNutrition(WITHIN, missing="garam masala")
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=nutrition, num_dinners=1)
    assert plan.recipes[0].title == "Spiced Chicken"  # 5 g missing spice tolerated
    assert not plan.recipes[0].flagged
    assert agent.replace_calls == 0  # no slow revise over a negligible ingredient


def test_large_unresolved_ingredient_still_revises(tmp_path):
    conn = _conn(tmp_path)
    dish = Recipe("Mystery Dish", 4,
                  [Ingredient("chicken breast", 400), Ingredient("exotic root", 200)], ["cook"])
    agent = FakeAgent([dish], replacement=_recipe_with("Clean Dish", "chicken breast"))
    nutrition = _PartialNutrition(WITHIN, missing="exotic root")
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=nutrition, num_dinners=1)
    assert agent.replace_calls == 1  # a 200 g gap could hide real macros -> revise
    assert plan.recipes[0].title == "Clean Dish"


def test_unresolved_ingredients_are_logged_for_review(tmp_path):
    conn = _conn(tmp_path)
    dish = Recipe("Exotic Bowl", 4, [
        Ingredient("chicken breast", 400),   # resolves
        Ingredient("dragon fruit", 100),     # missing, big enough to force a revise
        Ingredient("saffron", 2),            # missing, but negligible -> tolerated
    ], ["cook"])
    agent = FakeAgent([dish], replacement=_recipe_with("Clean", "chicken breast"))
    nutrition = _PartialNutrition(WITHIN, missing={"dragon fruit", "saffron"})
    generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=nutrition, num_dinners=1)

    logged = {r["name"]: r for r in conn.execute("SELECT * FROM missing_ingredients")}
    assert logged["dragon fruit"]["caused_revise"] == 1  # 100 g -> forced a revise
    assert logged["dragon fruit"]["sample_grams"] == 100
    assert logged["saffron"]["caused_revise"] == 0       # 2 g -> logged but tolerated
    assert "chicken breast" not in logged                # resolved ingredients aren't logged


def test_generate_plan_verifies_macros_and_persists(tmp_path):
    conn = _conn(tmp_path)
    agent = FakeAgent([_recipe("A"), _recipe("B")], replacement=_recipe("R"))
    plan = generate_plan(
        conn, agent=agent, offers=FakeOffers([FakeOffer("Kylling")]),
        nutrition=FakeNutrition(WITHIN), num_dinners=2,
    )
    assert [r.title for r in plan.recipes] == ["A", "B"]
    # 400g of 100kcal/100g over 4 servings -> 100 kcal / serving, within 600
    assert plan.recipes[0].per_serving.kcal == 100.0
    assert all(not r.flagged for r in plan.recipes)
    assert agent.replace_calls == 0


def test_out_of_target_dish_is_revised_then_accepted(tmp_path):
    conn = _conn(tmp_path)
    # first recipe is too hot; replacement is within target
    agent = FakeAgent([_recipe("Hot")], replacement=_recipe("Cool"))
    nutrition = _SwitchingNutrition(first=TOO_HOT, then=WITHIN)
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=nutrition, num_dinners=1)
    assert agent.replace_calls == 1
    assert plan.recipes[0].title == "Cool"
    assert not plan.recipes[0].flagged


def test_persistently_out_of_target_is_flagged_after_bounded_retries(tmp_path):
    conn = _conn(tmp_path)
    agent = FakeAgent([_recipe("Hot")], replacement=_recipe("StillHot"))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(TOO_HOT), num_dinners=1)
    assert plan.recipes[0].flagged
    assert agent.replace_calls <= 3  # bounded, no infinite loop


def test_plan_persists_and_reloads(tmp_path):
    conn = _conn(tmp_path)
    agent = FakeAgent([_recipe("A"), _recipe("B")], replacement=_recipe("R"))
    generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN), num_dinners=2)
    loaded = load_latest_plan(conn)
    assert loaded is not None
    assert [r.title for r in loaded.recipes] == ["A", "B"]
    assert loaded.recipes[0].ingredients[0].grams == 400.0
    assert loaded.recipes[0].steps == ["cook"]


def test_weekday_meals_assigns_one_dinner_per_weekday(tmp_path):
    conn = _conn(tmp_path)
    recipes = [_recipe(t) for t in ("A", "B", "C", "D", "E")]
    agent = FakeAgent(recipes, replacement=_recipe("R"))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN), num_dinners=5)
    meals = weekday_meals(plan.recipes)
    assert len(meals) == 5  # dinners only, no leftover-lunch expansion
    assert [m.day for m in meals] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert [m.title for m in meals] == ["A", "B", "C", "D", "E"]


def test_recipe_servings_match_preference_without_lunch_doubling(tmp_path):
    conn = _conn(tmp_path, servings=2)
    agent = FakeAgent([_recipe("A")], replacement=_recipe("R"))
    generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN), num_dinners=1)
    assert agent.last_ctx.servings == 2  # was doubled to 4 for lunches; now cooks just dinner


class _SwitchingNutrition:
    def __init__(self, first, then):
        self._first = first
        self._then = then
        self._calls = 0

    def lookup(self, name):
        self._calls += 1
        return (1, self._first if self._calls == 1 else self._then)


# --- display translation (English stays the lookup key; translation is display-only) ---


def test_translation_is_applied_and_persisted_when_language_is_set(tmp_path):
    conn = _conn(tmp_path, language="Danish")
    agent = FakeAgent([_recipe_with("Chicken Bowl", "chicken breast")], replacement=_recipe("R"))
    translated = Translated("Kyllingeskål", ["steg det"], ["kyllingebryst"])
    translator = FakeTranslator(result=translated)
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN),
                         translator=translator, num_dinners=1)
    r = plan.recipes[0]
    assert r.title == "Chicken Bowl"                 # English stays the canonical title
    assert r.title_translated == "Kyllingeskål"
    assert r.steps_translated == ["steg det"]
    assert r.ingredient_translations == {"chicken breast": "kyllingebryst"}
    assert translator.calls[0][3] == "Danish"        # language passed through

    reloaded = load_latest_plan(conn).recipes[0]      # survives persist/reload
    assert reloaded.title_translated == "Kyllingeskål"
    assert reloaded.ingredient_translations == {"chicken breast": "kyllingebryst"}


def test_no_translation_call_when_language_is_blank(tmp_path):
    conn = _conn(tmp_path, language="")  # default
    agent = FakeAgent([_recipe("A")], replacement=_recipe("R"))
    translator = FakeTranslator(result=Translated("X", ["y"], ["z"]))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN),
                         translator=translator, num_dinners=1)
    assert translator.calls == []                    # never called — no wasted spend when off
    assert plan.recipes[0].title_translated is None


def test_no_translation_when_translator_not_configured(tmp_path):
    conn = _conn(tmp_path, language="Danish")
    agent = FakeAgent([_recipe("A")], replacement=_recipe("R"))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN),
                         num_dinners=1)  # translator defaults to None
    assert plan.recipes[0].title_translated is None


def test_translation_failure_falls_back_to_english_without_breaking_the_plan(tmp_path):
    conn = _conn(tmp_path, language="Danish")
    agent = FakeAgent([_recipe("A")], replacement=_recipe("R"))
    translator = FakeTranslator(result=None)          # simulates malformed/mismatched LLM output
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN),
                         translator=translator, num_dinners=1)
    assert plan.recipes[0].title == "A"
    assert plan.recipes[0].title_translated is None


def test_translator_exception_falls_back_to_english_without_breaking_the_plan(tmp_path):
    conn = _conn(tmp_path, language="Danish")
    agent = FakeAgent([_recipe("A")], replacement=_recipe("R"))
    translator = FakeTranslator(raises=RuntimeError("network blip"))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN),
                         translator=translator, num_dinners=1)
    assert plan.recipes[0].title == "A"
    assert plan.recipes[0].title_translated is None


def test_decline_translates_the_replacement(tmp_path):
    conn = _conn(tmp_path, language="Danish")
    agent = FakeAgent([_recipe("A"), _recipe("B")], replacement=_recipe_with("Fish Dish", "cod"))
    plan = generate_plan(conn, agent=agent, offers=FakeOffers([]), nutrition=FakeNutrition(WITHIN),
                         num_dinners=2)  # initial plan generated without translation configured yet
    translator = FakeTranslator(result=Translated("Fiskeret", ["steg torsken"], ["torsk"]))
    updated = decline(conn, plan.id, 0, agent=agent, offers=FakeOffers([]),
                      nutrition=FakeNutrition(WITHIN), translator=translator)
    assert updated.recipes[0].title_translated == "Fiskeret"
    assert updated.recipes[1].title_translated is None  # untouched sibling stays as it was


# --- print compact-mode threshold (app/planner.py) ---

WITHIN_MACROS = Macros(500, 40, 20, 15)


def _print_compact_recipe(n_ingredients: int, n_steps: int) -> PlannedRecipe:
    ingredients = [Ingredient(f"ingredient {i}", 10) for i in range(n_ingredients)]
    steps = [f"Step {i} of the recipe." for i in range(n_steps)]
    return PlannedRecipe("Test dish", 4, ingredients, steps, WITHIN_MACROS, False)


def test_is_print_compact_false_for_realistic_recipe():
    assert is_print_compact(_print_compact_recipe(13, 9)) is False  # observed real max: 13 ingredients, 9 steps


def test_is_print_compact_true_at_ingredient_threshold():
    assert is_print_compact(_print_compact_recipe(14, 5)) is True


def test_is_print_compact_true_at_step_threshold():
    assert is_print_compact(_print_compact_recipe(5, 10)) is True


def test_is_print_compact_false_just_below_both_thresholds():
    assert is_print_compact(_print_compact_recipe(13, 9)) is False
