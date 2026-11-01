"""Provider-neutral meal-plan agent.

Deliberately NOT tied to any single LLM vendor. `OpenAICompatibleAgent` speaks the
OpenAI `/chat/completions` shape, which every major provider exposes — Gemini, DeepSeek,
Qwen, Kimi, GLM, OpenRouter, local Ollama/vLLM, and Anthropic's compat endpoint — so the
model is swapped by changing `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`, no code change.

Structured output is obtained by prompting for JSON, then parsing + validating + a bounded
retry — which works on any chat model, not only those with native JSON-schema support. The
agent is never trusted for macros: it emits {name, grams} ingredients that the deterministic
`nutrition` slice verifies.
"""

from dataclasses import dataclass, field, replace
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from app import telemetry
from app.bans import ban_display
from app.llm import chat_completion, extract_json_object, with_retry
from app.nutrition import Ingredient


@dataclass(frozen=True)
class Recipe:
    title: str
    servings: int
    ingredients: list[Ingredient]
    steps: list[str]
    source: str = ""  # URL of the real recipe this was adapted from (grounded agent only)
    cuisine: str = ""        # model's own label, lowercased: keeps one plan from repeating itself
    main_protein: str = ""   # likewise ("chicken", "lentils", ...)


@dataclass(frozen=True)
class PlanContext:
    max_kcal: int | None
    min_protein_g: int | None
    restrictions: str
    servings: int
    num_dinners: int
    offers: list[str]           # Danish offer names to bias the plan toward
    preference_summary: str     # liked/disliked signal from ratings (T11 fills this)
    bans: list[str] = field(default_factory=list)  # banned category ids + custom items
    avoid_dishes: list[str] = field(default_factory=list)  # recent dinners to not repeat
    use_up: str = ""            # free-text ingredients the user already has and wants to use
    plan_dishes: list[str] = field(default_factory=list)  # "Title (cuisine, protein)" already in this plan


class MealPlanAgent(Protocol):
    def plan_week(self, ctx: PlanContext) -> list[Recipe]: ...
    def replace(self, declined: Recipe, ctx: PlanContext) -> Recipe: ...


class RecipeError(ValueError):
    """The agent produced output that could not be parsed into valid Recipe(s)."""


_SYSTEM = (
    "You are a meal-planning assistant for a home cook. You output ONLY JSON, no prose, no "
    "markdown. Write dinners people look forward to: real, satisfying home cooking with proper "
    "flavour, not diet food. Design each dinner on its own merits; never distort a recipe just to "
    "use a deal. Follow this priority order strictly:\n"
    "1) MACROS (hard requirement): each serving lands in the given calorie range. The calories "
    "are a target, not a limit: aim close to it rather than far under. Each serving also meets "
    "the protein floor. Reach protein with normal portions "
    "of meat, fish, eggs, dairy or legumes, and don't overshoot it by much. Never pad protein or "
    "cut calories with diet swaps such as egg whites, low-fat or fat-free dairy, extra-lean mince "
    "or cauliflower rice.\n"
    "2) A BALANCED PLATE: most dinners have a filling carb base (rice, potatoes, pasta, bread, "
    "whole grains, beans or lentils) and enough fat to taste good (olive oil, butter, cheese, "
    "yogurt, nuts, avocado, coconut milk). PCOS-friendly does not mean low-carb or low-fat: prefer "
    "slower carbs (whole grains, legumes, potatoes with skin) and plenty of vegetables.\n"
    "3) FLAVOUR: build every dish on aromatics, spices or herbs, an acid (lemon, lime, vinegar) "
    "and a sauce, dressing or topping. Pantry spices, sauces and condiments don't count against "
    "simplicity.\n"
    "4) TIME: keep it SIMPLE and FAST, about 30 min or less and few steps. No one wants an "
    "hour-long project on a weeknight.\n"
    "5) MONEY: be cost-conscious. Lean on affordable everyday proteins common in Denmark "
    "(chicken, pork, minced pork or chicken, eggs, canned or frozen fish, legumes), and treat "
    "pricier meats like beef and veal as an occasional choice rather than a weekly staple. They "
    "are welcome, especially when on offer, just not the default every time. Use any listed "
    "on-offer items opportunistically where they fit a dish that already satisfies the above.\n"
    "Also optimise for fibre (vegetables, legumes, whole grains) where it doesn't conflict with "
    "the above. Higher fibre is better, but it is NOT a hard requirement.\n"
    "VARIETY: deliberately vary the meals. Rotate the main protein (poultry, pork, beef/veal, "
    "eggs, fish/seafood, legumes/vegetarian), the cooking method (pan-fry, roast, stir-fry, "
    "grill, stew, sheet-pan, salad), the cuisine, the carb base and the vegetables (not broccoli, "
    "cabbage or cauliflower every night). Do not lean on the same one or two proteins, the same "
    "technique, or the same high-protein crutch ingredient repeatedly. Skip any protein the user "
    "has banned.\n"
    "Ingredient quantities MUST be in grams so calories can be computed exactly. Use generic "
    "English ingredient names for lookup (e.g. 'trout fillet', 'chicken breast'), never brand "
    "names or parenthetical notes."
)

KCAL_TOLERANCE = 0.10  # the kcal preference is a target: a dinner within 10% either way is on target


def _constraints(ctx: PlanContext) -> str:
    parts = [f"Servings per recipe: {ctx.servings}."]
    if ctx.max_kcal is not None:
        # Aim a little under the target: the computed kcal drifts from the model's estimate, and
        # landing over the band costs a revise while landing a bit under doesn't.
        aim = round(ctx.max_kcal * (1 - KCAL_TOLERANCE / 2))
        low = round(ctx.max_kcal * (1 - KCAL_TOLERANCE))
        high = round(ctx.max_kcal * (1 + KCAL_TOLERANCE))
        parts.append(f"Calories: aim for {aim}-{ctx.max_kcal} kcal per serving ({low}-{high} "
                     "counts as on target).")
    if ctx.min_protein_g is not None:
        parts.append(f"At least {ctx.min_protein_g} g protein per serving.")
    if ctx.restrictions.strip():
        parts.append(f"Dietary restrictions: {ctx.restrictions}.")
    if ctx.bans:
        parts.append(
            "NEVER include any of these banned foods, or ingredients made from them: "
            + ", ".join(ban_display(ctx.bans)) + "."
        )
    if ctx.avoid_dishes:
        parts.append(
            "Do NOT repeat these recent dinners — make clearly different meals: "
            + "; ".join(ctx.avoid_dishes) + "."
        )
    if ctx.plan_dishes:
        parts.append(
            "Already in this plan, so use a different cuisine or main protein: "
            + "; ".join(ctx.plan_dishes) + "."
        )
    if ctx.use_up.strip():
        parts.append(
            "The user already has these ingredients and wants to use them up this week: "
            + ctx.use_up.strip()
            + ". Build as many dinners as sensibly possible around them — for THIS week it is "
            "fine to reuse a main ingredient (e.g. the same protein) across several dinners to "
            "use it up; keep the cooking methods and cuisines varied instead."
        )
    if ctx.offers:
        parts.append("Optional deals to use only where they fit: " + ", ".join(ctx.offers) + ".")
    if ctx.preference_summary.strip():
        parts.append(f"User preferences: {ctx.preference_summary}.")
    return " ".join(parts)


_RECIPE_SHAPE = (
    '{"title": str, "cuisine": str, "main_protein": str, "servings": int, '
    '"ingredients": [{"name": str, "grams": number}], "steps": [str]}'
)


def _plan_prompt(ctx: PlanContext) -> str:
    return (
        f"Create {ctx.num_dinners} distinct weekday dinners — each a different main protein and "
        f"cooking method, spanning different cuisines. {_constraints(ctx)} "
        f'Respond with JSON: {{"recipes": [{_RECIPE_SHAPE}, ...]}}'
    )


def _replace_prompt(declined: Recipe, ctx: PlanContext) -> str:
    return (
        f"Replace the dinner '{declined.title}' with one different dinner. "
        f"{_constraints(ctx)} Respond with JSON: {_RECIPE_SHAPE}"
    )


def _extract_json(content: str) -> dict:
    obj = extract_json_object(content)
    if not obj:
        raise RecipeError("no valid JSON object found in response")
    return obj


def _parse_recipe(obj: object) -> Recipe:
    if not isinstance(obj, dict):
        raise RecipeError("recipe is not an object")
    try:
        ingredients = [
            Ingredient(name=str(i["name"]), grams=float(i["grams"])) for i in obj["ingredients"]
        ]
        recipe = Recipe(
            title=str(obj["title"]),
            servings=int(obj["servings"]),
            ingredients=ingredients,
            steps=[str(s) for s in obj["steps"]],
            source=str(obj.get("source") or "").strip(),
            cuisine=str(obj.get("cuisine") or "").strip().lower(),
            main_protein=str(obj.get("main_protein") or "").strip().lower(),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise RecipeError(f"malformed recipe: {e}") from e
    if not recipe.title or not recipe.ingredients or not recipe.steps:
        raise RecipeError("recipe missing title, ingredients, or steps")
    return recipe


def _plan_build(data: dict) -> list[Recipe]:
    return [_parse_recipe(r) for r in data["recipes"]]


def _generate_with_retry(call, user_prompt: str, build, max_attempts: int):
    """`call(turns) -> content`, where turns is a list of (role, text). Parse the JSON and
    build the result; on invalid output append a correction and retry (bounded)."""
    turns: list[tuple[str, str]] = [("user", user_prompt)]
    last_error: Exception | None = None
    for _ in range(max_attempts):
        content = call(turns)
        try:
            result = build(_extract_json(content))
            if not result:
                raise RecipeError("no recipes returned")
            return result
        except (RecipeError, KeyError, TypeError) as e:
            last_error = e
            telemetry.bump_retry()  # a re-prompt is another billed call
            turns = [
                *turns,
                ("assistant", content),
                ("user", f"That was invalid ({e}). Return ONLY the JSON."),
            ]
    raise RecipeError(f"agent failed after {max_attempts} attempts: {last_error}")


class OpenAICompatibleAgent:
    """Any OpenAI-compatible provider (Gemini/DeepSeek/Qwen/OpenRouter/local/…). No grounding."""

    def __init__(self, base_url, model, api_key, client: httpx.Client | None = None, max_attempts=3):
        self.model = model
        self.max_attempts = max_attempts
        self._client = client or httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=120
        )

    def plan_week(self, ctx: PlanContext) -> list[Recipe]:
        with telemetry.record("plan_week", "openai-compat", self.model):
            recipes = _generate_with_retry(self._call, _plan_prompt(ctx), _plan_build,
                                           self.max_attempts)
        return [_without_source(r) for r in recipes]

    def replace(self, declined: Recipe, ctx: PlanContext) -> Recipe:
        with telemetry.record("replace", "openai-compat", self.model):
            recipe = _generate_with_retry(self._call, _replace_prompt(declined, ctx),
                                          _parse_recipe, self.max_attempts)
        return _without_source(recipe)

    def _call(self, turns: list[tuple[str, str]]) -> str:
        messages = [{"role": "system", "content": _SYSTEM}]
        messages += [{"role": role, "content": text} for role, text in turns]
        return chat_completion(self._client, self.model, messages)


# Gemini won't use Google Search while it's told to answer in JSON, so planning is two calls: a
# plain-language search for real recipes, then the JSON plan built on what it found.
_ADAPT = (
    "\nREAL RECIPES: base each dinner on one of the real, well-reviewed recipes listed in the "
    "request, then adapt it to the calorie range and protein floor by adjusting portions and "
    "making small swaps. Keep what makes the dish itself: its real name (e.g. 'Chicken tikka "
    "masala with brown rice', 'Pasta e fagioli'), its sauce, spices and method. Never invent a "
    "dish and title it as a list of its components. Add a \"source\" field with the URL of the "
    "recipe page you adapted, exactly as listed, or \"\" if you didn't use one."
)


MAX_SEARCHES = 3  # each Google Search query is billed (~$0.014), so ask for a few broad ones


def _search_prompt(ctx: PlanContext) -> str:
    return (
        f"Use Google Search, at most {MAX_SEARCHES} searches in total (broad ones, e.g. "
        "'healthy high protein dinner recipes'), to find "
        f"{ctx.num_dinners + 3} real, well-reviewed recipes for "
        "weeknight dinners that could fit these needs once portions are adjusted. Pick "
        "different cuisines and different main proteins, each with a filling carb base. "
        f"{_constraints(ctx)} For each recipe, give the dish name, its cuisine, its main "
        "protein and the URL of the recipe page."
    )


def _without_source(recipe: Recipe) -> Recipe:
    return replace(recipe, source="") if recipe.source else recipe


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def _verified_source(recipe: Recipe, searched: set[str]) -> Recipe:
    """Keep a source URL only if it's an http(s) link on a site that appeared in the plan's
    Google Search results, so the model can't hand us an invented link."""
    url = recipe.source
    if not url:
        return recipe
    host = _host(url) if urlsplit(url).scheme in ("http", "https") else ""
    if host and any(host == d or host.endswith("." + d) for d in searched):
        return recipe
    return replace(recipe, source="")


def _searched_domains(grounding: dict) -> set[str]:
    """Sites in a Gemini response's search results (chunk titles are the site's domain)."""
    domains = set()
    for chunk in grounding.get("groundingChunks") or []:
        title = ((chunk.get("web") or {}).get("title") or "").strip().lower()
        if title and " " not in title and "." in title:
            domains.add(title.removeprefix("www."))
    return domains


class GeminiGroundedAgent:
    """Gemini via its NATIVE generateContent API. Planning first searches Google for real
    recipes, then adapts them to the user's targets; swaps and revisions skip the search.

    Same MealPlanAgent interface: searching is a provider-specific implementation detail.
    Macros are still computed deterministically from the local store, never from the model.
    """

    def __init__(self, base_url, model, api_key, client: httpx.Client | None = None, max_attempts=3):
        # base_url is the NATIVE base, e.g. https://generativelanguage.googleapis.com/v1beta
        self.model = model
        self.max_attempts = max_attempts
        self._client = client or httpx.Client(
            base_url=base_url, headers={"x-goog-api-key": api_key}, timeout=120
        )

    def plan_week(self, ctx: PlanContext) -> list[Recipe]:
        found, searched = self._search(ctx)
        prompt = _plan_prompt(ctx)
        if found:
            prompt = f"Real recipes found for this plan:\n{found}\n\n{prompt}"
        with telemetry.record("plan_week", "gemini", self.model):
            recipes = _generate_with_retry(lambda t: self._call(t, _SYSTEM + _ADAPT), prompt,
                                           _plan_build, self.max_attempts)
        return [_verified_source(r, searched) for r in recipes]

    def replace(self, declined: Recipe, ctx: PlanContext) -> Recipe:
        # No search: swaps and revisions stay fast (a search call adds ~10-15 s each), so they
        # carry no source link.
        with telemetry.record("replace", "gemini", self.model):
            recipe = _generate_with_retry(lambda t: self._call(t, _SYSTEM),
                                          _replace_prompt(declined, ctx), _parse_recipe,
                                          self.max_attempts)
        return _without_source(recipe)

    def _search(self, ctx: PlanContext) -> tuple[str, set[str]]:
        """Plain-language Google Search for real recipes: (the answer text, the sites it
        searched). Best-effort: if it fails, the plan is written without references."""
        try:
            with telemetry.record("recipe_search", "gemini", self.model, grounded=True):
                text, grounding = self._generate(
                    [("user", _search_prompt(ctx))], system=None, search=True)
        except Exception:  # noqa: BLE001 - a failed search must not block planning
            return "", set()
        return text, _searched_domains(grounding)

    def _call(self, turns: list[tuple[str, str]], system: str) -> str:
        return self._generate(turns, system=system, search=False)[0]

    def _generate(self, turns: list[tuple[str, str]], *, system: str | None,
                  search: bool) -> tuple[str, dict]:
        """One generateContent call: (text, groundingMetadata)."""
        body: dict = {"contents": [
            {"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
            for role, text in turns
        ]}
        if system is not None:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if search:
            body["tools"] = [{"google_search": {}}]

        def call() -> tuple[str, dict]:
            resp = self._client.post(f"/models/{self.model}:generateContent", json=body)
            resp.raise_for_status()
            data = resp.json()
            cand = data["candidates"][0]
            usage = data.get("usageMetadata") or {}
            grounding = cand.get("groundingMetadata") or {}
            telemetry.set_usage(
                input_tokens=usage.get("promptTokenCount"),
                output_tokens=usage.get("candidatesTokenCount"),
                thinking_tokens=usage.get("thoughtsTokenCount"),
                total_tokens=usage.get("totalTokenCount"),
                search_queries=len(grounding.get("webSearchQueries") or []),
                finish_reason=cand.get("finishReason"),
            )
            return "".join(part.get("text", "") for part in cand["content"]["parts"]), grounding

        return with_retry(call)
