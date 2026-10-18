"""Weekly-plan flow: assemble context, generate dinners, verify macros deterministically,
revise out-of-target dishes (bounded), persist, and expand into weekday meals.

The agent is untrusted for numbers — every recipe's per-serving macros are recomputed by
`nutrition.macros_for` and checked against the user's targets here.
"""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import psycopg

from app import telemetry
from app.agent import MealPlanAgent, PlanContext, Recipe
from app.bans import violates
from app.dates import iso_week
from app.history import AVOID_PROMPT_CAP, PastDish, is_repeat, recent_avoided
from app.nutrition import Ingredient, Macros, NutritionSource, macros_for
from app.offers import OffersProvider
from app.profile import load_preferences, preference_summary, selected_store_slugs
from app.translate import Translator

MAX_REVISIONS = 2       # bounded revise loop per dish
OFFER_PROMPT_CAP = 40   # how many offer names to hand the agent
# Small slack so a near-miss (e.g. 39 g vs a 40 g floor) isn't chased with a slow LLM revise —
# the macros are estimates from a food store, not lab values. Gross misses still get revised.
KCAL_SLACK = 40         # allow this many kcal over the ceiling
PROTEIN_SLACK = 3       # allow this many grams under the floor
# An unresolved ingredient this small (a spice/herb/aromatic the food store lacks) has
# negligible macros — don't reject the whole dish over it. Bigger gaps still force a revise.
UNRESOLVED_GRAMS = 40
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


@dataclass(frozen=True)
class PlannedRecipe:
    title: str
    servings: int
    ingredients: list[Ingredient]
    steps: list[str]
    per_serving: Macros
    flagged: bool  # macros couldn't be verified within targets (or ingredients unresolved)
    # Display-only translation (app/translate.py). English above stays the USDA/offer lookup key;
    # these are populated once at persist time when a display language is configured, else None.
    title_translated: str | None = None
    steps_translated: list[str] | None = None
    ingredient_translations: dict[str, str] | None = None  # English name -> translated name


def is_print_compact(recipe: PlannedRecipe) -> bool:
    """Whether a printed A5 recipe page needs the compact CSS tier to avoid overflowing one
    page. Thresholds are set above the largest ingredient/step counts this app has ever
    generated (13 ingredients, 9 steps), validated by direct measurement against real A5
    content-box dimensions."""
    return len(recipe.ingredients) >= 14 or len(recipe.steps) >= 10


@dataclass(frozen=True)
class Plan:
    id: int
    recipes: list[PlannedRecipe]
    use_up: str = ""  # ingredients this plan was asked to use up


@dataclass(frozen=True)
class Meal:
    day: str
    title: str


def _per_serving(recipe: Recipe, source: NutritionSource) -> tuple[Macros, bool, list[str]]:
    """Return per-serving macros, whether the recipe resolved well enough to trust them, and the
    names the food store couldn't resolve.

    A tiny unresolved ingredient (a missing spice/herb) barely moves the macros, so it doesn't
    count as unresolved — only a materially-sized gap (>= UNRESOLVED_GRAMS) does."""
    result = macros_for(recipe.ingredients, source)
    n = max(recipe.servings, 1)
    per = Macros(
        kcal=result.total.kcal / n,
        protein_g=result.total.protein_g / n,
        carbs_g=result.total.carbs_g / n,
        fat_g=result.total.fat_g / n,
        fibre_g=result.total.fibre_g / n,
    )
    unresolved = set(result.unresolved)
    material_gap = any(i.name in unresolved and i.grams >= UNRESOLVED_GRAMS for i in recipe.ingredients)
    return per, not material_gap, result.unresolved


def _log_missing(conn: psycopg.Connection, recipe: Recipe, unresolved: list[str]) -> None:
    """Record ingredients the food store couldn't resolve, for later batch-adding with real
    macros. Rolled up by name: total hits, how often it was big enough to force a revise."""
    missing = set(unresolved)
    now = datetime.now(UTC)
    with conn.transaction():
        for ing in recipe.ingredients:
            if ing.name in missing:
                conn.execute(
                    "INSERT INTO missing_ingredients (name, hits, caused_revise, sample_grams, last_seen) "
                    "VALUES (%s, 1, %s, %s, %s) "
                    "ON CONFLICT(name) DO UPDATE SET hits = missing_ingredients.hits + 1, "
                    "caused_revise = missing_ingredients.caused_revise + excluded.caused_revise, "
                    "sample_grams = excluded.sample_grams, last_seen = excluded.last_seen",
                    (ing.name.strip().lower(), int(ing.grams >= UNRESOLVED_GRAMS), ing.grams, now),
                )


def _within_targets(per: Macros, ctx: PlanContext) -> bool:
    over_kcal = ctx.max_kcal is not None and per.kcal > ctx.max_kcal + KCAL_SLACK
    under_protein = ctx.min_protein_g is not None and per.protein_g < ctx.min_protein_g - PROTEIN_SLACK
    return not (over_kcal or under_protein)


def _verify_or_revise(
    recipe: Recipe, ctx: PlanContext, agent: MealPlanAgent, source: NutritionSource,
    past: list[PastDish] = (), conn: psycopg.Connection | None = None,
) -> PlannedRecipe:
    current = recipe
    for _ in range(MAX_REVISIONS + 1):
        per, resolved, unresolved = _per_serving(current, source)
        if conn is not None and unresolved:
            _log_missing(conn, current, unresolved)
        if (resolved and _within_targets(per, ctx)
                and not violates(current.ingredients, ctx.bans)
                and not is_repeat(current.title, current.ingredients, past)):
            return PlannedRecipe(current.title, current.servings, current.ingredients,
                                 current.steps, per, flagged=False)
        current = agent.replace(current, ctx)
    # exhausted the revise budget — keep the last attempt but flag for human review
    per, _, unresolved = _per_serving(current, source)
    if conn is not None and unresolved:
        _log_missing(conn, current, unresolved)
    return PlannedRecipe(current.title, current.servings, current.ingredients,
                         current.steps, per, flagged=True)


def _translate(planned: PlannedRecipe, language: str, translator: Translator) -> PlannedRecipe:
    """Translate a finalized recipe for display. Never blocks the plan: any failure (bad
    output, network error, whatever) just keeps the recipe in English."""
    names = [i.name for i in planned.ingredients]
    try:
        result = translator.translate(planned.title, planned.steps, names, language)
    except Exception:  # noqa: BLE001 - a translation hiccup must not break the plan
        return planned
    if result is None:
        return planned
    return replace(
        planned,
        title_translated=result.title,
        steps_translated=result.steps,
        ingredient_translations=dict(zip(names, result.ingredient_names)),
    )


def _plan_context(
    conn: psycopg.Connection, user_id: int, offers: OffersProvider, num_dinners: int,
    past: list[PastDish] = (), use_up: str = "",
) -> PlanContext:
    prefs = load_preferences(conn, user_id)
    selected = selected_store_slugs(conn, user_id)
    offer_names = [o.name for o in offers.offers(selected)][:OFFER_PROMPT_CAP] if selected else []
    return PlanContext(
        max_kcal=prefs.max_kcal,
        min_protein_g=prefs.min_protein_g,
        restrictions=prefs.restrictions,
        servings=prefs.servings,  # dinner only — cook exactly for the household
        num_dinners=num_dinners,
        offers=offer_names,
        preference_summary=preference_summary(conn, user_id),
        bans=prefs.bans,
        avoid_dishes=[p.title for p in past][:AVOID_PROMPT_CAP],
        use_up=use_up,
    )


def generate_plan(
    conn: psycopg.Connection,
    user_id: int,
    *,
    agent: MealPlanAgent,
    offers: OffersProvider,
    nutrition: NutritionSource,
    translator: Translator | None = None,
    num_dinners: int = 5,
    use_up: str = "",
) -> Plan:
    with telemetry.session(tenant=str(user_id)) as trace_id:
        past = recent_avoided(conn, user_id)  # recent (non-liked) + disliked dinners to not repeat
        ctx = _plan_context(conn, user_id, offers, num_dinners, past, use_up)
        recipes = agent.plan_week(ctx)
        if hasattr(nutrition, "warm"):  # resolve all ingredients' macros in one parallel pass
            nutrition.warm([i.name for r in recipes for i in r.ingredients])
        verified = [_verify_or_revise(r, ctx, agent, nutrition, past, conn) for r in recipes]
        language = load_preferences(conn, user_id).language
        if translator is not None and language:
            verified = [_translate(r, language, translator) for r in verified]
        plan_id = _persist(conn, user_id, verified, use_up)
        telemetry.link_plan(conn, trace_id, plan_id)
    return Plan(id=plan_id, recipes=verified, use_up=use_up)


def decline(
    conn: psycopg.Connection,
    user_id: int,
    plan_id: int,
    slot_index: int,
    *,
    agent: MealPlanAgent,
    offers: OffersProvider,
    nutrition: NutritionSource,
    translator: Translator | None = None,
) -> Plan:
    """Replace one dish with a fresh, macro-verified alternative and re-persist the plan."""
    plan = load_plan(conn, user_id, plan_id)
    if not 0 <= slot_index < len(plan.recipes):  # not this user's plan, or no such dish
        raise LookupError(f"plan {plan_id} has no dish {slot_index} for this user")
    with telemetry.session(tenant=str(user_id)) as trace_id:
        # avoid recent/disliked dinners AND the other dishes already in this plan
        siblings = [PastDish.of(r.title, [i.name for i in r.ingredients])
                    for i, r in enumerate(plan.recipes) if i != slot_index]
        past = recent_avoided(conn, user_id) + siblings
        ctx = _plan_context(conn, user_id, offers, num_dinners=len(plan.recipes), past=past,
                            use_up=plan.use_up)
        declined = _to_recipe(plan.recipes[slot_index])
        replacement = _verify_or_revise(agent.replace(declined, ctx), ctx, agent, nutrition, past, conn)
        language = load_preferences(conn, user_id).language
        if translator is not None and language:
            replacement = _translate(replacement, language, translator)
        telemetry.link_plan(conn, trace_id, plan_id)
    recipes = list(plan.recipes)
    recipes[slot_index] = replacement
    _rewrite_plan(conn, plan_id, recipes)
    return Plan(id=plan_id, recipes=recipes, use_up=plan.use_up)


def _to_recipe(planned: PlannedRecipe) -> Recipe:
    return Recipe(
        title=planned.title,
        servings=planned.servings,
        ingredients=planned.ingredients,
        steps=planned.steps,
    )


def _persist(conn: psycopg.Connection, user_id: int, recipes: list[PlannedRecipe],
             use_up: str = "") -> int:
    with conn.transaction():  # a plan and all its dishes land together, or not at all
        plan_id = conn.execute(
            "INSERT INTO plans (user_id, created_at, week, use_up) VALUES (%s, %s, %s, %s) "
            "RETURNING id",
            (user_id, datetime.now(UTC), iso_week(datetime.now(UTC).date()), use_up),
        ).fetchone()["id"]
        _insert_recipes(conn, plan_id, recipes)
    return plan_id


def _insert_recipes(conn: psycopg.Connection, plan_id: int, recipes: list[PlannedRecipe]) -> None:
    for slot, r in enumerate(recipes):
        recipe_id = conn.execute(
            """INSERT INTO recipes
               (plan_id, slot_index, title, title_translated, servings, steps, steps_translated,
                per_serving_kcal, per_serving_protein_g, per_serving_carbs_g, per_serving_fat_g,
                per_serving_fibre_g, flagged)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (plan_id, slot, r.title, r.title_translated, r.servings, json.dumps(r.steps),
             json.dumps(r.steps_translated) if r.steps_translated is not None else None,
             r.per_serving.kcal, r.per_serving.protein_g, r.per_serving.carbs_g,
             r.per_serving.fat_g, r.per_serving.fibre_g, bool(r.flagged)),
        ).fetchone()["id"]
        translations = r.ingredient_translations or {}
        conn.cursor().executemany(
            "INSERT INTO recipe_ingredients (recipe_id, name, name_translated, grams) "
            "VALUES (%s, %s, %s, %s)",
            [(recipe_id, i.name, translations.get(i.name), i.grams) for i in r.ingredients],
        )


def _rewrite_plan(conn: psycopg.Connection, plan_id: int, recipes: list[PlannedRecipe]) -> None:
    with conn.transaction():  # never leave a plan with its dishes half-replaced
        conn.execute(
            "DELETE FROM recipe_ingredients WHERE recipe_id IN "
            "(SELECT id FROM recipes WHERE plan_id = %s)", (plan_id,))
        conn.execute("DELETE FROM recipes WHERE plan_id = %s", (plan_id,))
        _insert_recipes(conn, plan_id, recipes)


def load_latest_plan(conn: psycopg.Connection, user_id: int) -> Plan | None:
    row = conn.execute(
        "SELECT id FROM plans WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    if row is None:
        return None
    return load_plan(conn, user_id, row["id"])


def load_plan(conn: psycopg.Connection, user_id: int, plan_id: int) -> Plan:
    """The user's plan; another user's (or a missing) plan comes back empty."""
    prow = conn.execute(
        "SELECT use_up FROM plans WHERE id = %s AND user_id = %s", (plan_id, user_id)).fetchone()
    if prow is None:
        return Plan(id=plan_id, recipes=[], use_up="")
    use_up = prow["use_up"]
    recipes = []
    for r in conn.execute(
        "SELECT * FROM recipes WHERE plan_id = %s ORDER BY slot_index", (plan_id,)
    ):
        ingredients = []
        translations = {}
        for i in conn.execute(
            "SELECT name, name_translated, grams FROM recipe_ingredients WHERE recipe_id = %s ORDER BY id",
            (r["id"],),
        ):
            ingredients.append(Ingredient(name=i["name"], grams=i["grams"]))
            if i["name_translated"]:
                translations[i["name"]] = i["name_translated"]
        recipes.append(
            PlannedRecipe(
                title=r["title"],
                servings=r["servings"],
                ingredients=ingredients,
                steps=json.loads(r["steps"]),
                per_serving=Macros(
                    r["per_serving_kcal"], r["per_serving_protein_g"],
                    r["per_serving_carbs_g"], r["per_serving_fat_g"],
                    r["per_serving_fibre_g"],
                ),
                flagged=bool(r["flagged"]),
                title_translated=r["title_translated"],
                steps_translated=json.loads(r["steps_translated"]) if r["steps_translated"] else None,
                ingredient_translations=translations or None,
            )
        )
    return Plan(id=plan_id, recipes=recipes, use_up=use_up)


def weekday_meals(recipes: list[PlannedRecipe]) -> list[Meal]:
    """Assign each dinner to a weekday (Mon–Fri). 5 dinners -> 5 weekday dinners."""
    return [Meal(day=day, title=recipes[i].title_translated or recipes[i].title)
            for i, day in enumerate(_WEEKDAYS[:len(recipes)])]
