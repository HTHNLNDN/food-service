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

from dataclasses import dataclass, field
from typing import Protocol

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


class MealPlanAgent(Protocol):
    def plan_week(self, ctx: PlanContext) -> list[Recipe]: ...
    def replace(self, declined: Recipe, ctx: PlanContext) -> Recipe: ...


class RecipeError(ValueError):
    """The agent produced output that could not be parsed into valid Recipe(s)."""


_SYSTEM = (
    "You are a meal-planning assistant. You output ONLY JSON, no prose, no markdown. "
    "Design good weeknight dinners on their own merits; never distort a recipe just to use a "
    "deal. Follow this priority order strictly:\n"
    "1) MACROS (hard requirement): PCOS-friendly when requested — hit the calorie ceiling and "
    "protein floor per serving. This is non-negotiable.\n"
    "2) TIME: keep it SIMPLE and FAST — about 30 min or less, a short ingredient list, and few "
    "steps. No one wants an hour-long project on a weeknight.\n"
    "3) MONEY: be cost-conscious. Lean on affordable everyday proteins common in Denmark "
    "(chicken, pork, minced pork or chicken, eggs, canned or frozen fish, legumes), and treat "
    "pricier meats like beef and veal as an occasional choice rather than a weekly staple — "
    "they are welcome, especially when on offer, just not the default every time. Use any "
    "listed on-offer items opportunistically where they fit a dish that already satisfies (1) "
    "and (2).\n"
    "Also optimise for fibre (favour vegetables, legumes, whole grains) where it doesn't "
    "conflict with the above — higher fibre is better, but it is NOT a hard requirement.\n"
    "VARIETY: deliberately vary the meals — rotate the main protein (poultry, pork, beef/veal, "
    "eggs, fish/seafood, legumes/vegetarian), the cooking method (pan-fry, roast, stir-fry, "
    "grill, stew, sheet-pan, salad), and the cuisine. Do not lean on the same one or two "
    "proteins, the same technique, or the same high-protein crutch ingredient repeatedly. Skip "
    "any protein the user has banned.\n"
    "Ingredient quantities MUST be in grams so calories can be computed exactly. Use generic "
    "English ingredient names for lookup (e.g. 'trout fillet', 'chicken breast') — never brand "
    "names or parenthetical notes."
)


def _constraints(ctx: PlanContext) -> str:
    parts = [f"Servings per recipe: {ctx.servings}."]
    if ctx.max_kcal is not None:
        parts.append(f"Max {ctx.max_kcal} kcal per serving.")
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
    '{"title": str, "servings": int, '
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
            return _generate_with_retry(self._call, _plan_prompt(ctx), _plan_build, self.max_attempts)

    def replace(self, declined: Recipe, ctx: PlanContext) -> Recipe:
        with telemetry.record("replace", "openai-compat", self.model):
            return _generate_with_retry(self._call, _replace_prompt(declined, ctx), _parse_recipe,
                                        self.max_attempts)

    def _call(self, turns: list[tuple[str, str]]) -> str:
        messages = [{"role": "system", "content": _SYSTEM}]
        messages += [{"role": role, "content": text} for role, text in turns]
        return chat_completion(self._client, self.model, messages)


_GROUNDED_SYSTEM = _SYSTEM + (
    "\nBase each dinner on real, well-reviewed recipes you find via Google Search."
)


class GeminiGroundedAgent:
    """Gemini via its NATIVE generateContent API with Google Search grounding.

    Same MealPlanAgent interface — grounding is a provider-specific implementation detail.
    The model searches/uses real recipes; macros are still computed deterministically from
    the local store, never from the model.
    """

    def __init__(self, base_url, model, api_key, client: httpx.Client | None = None, max_attempts=3):
        # base_url is the NATIVE base, e.g. https://generativelanguage.googleapis.com/v1beta
        self.model = model
        self.max_attempts = max_attempts
        self._client = client or httpx.Client(
            base_url=base_url, headers={"x-goog-api-key": api_key}, timeout=120
        )

    def plan_week(self, ctx: PlanContext) -> list[Recipe]:
        with telemetry.record("plan_week", "gemini", self.model, grounded=True):
            return _generate_with_retry(self._call, _plan_prompt(ctx), _plan_build, self.max_attempts)

    def replace(self, declined: Recipe, ctx: PlanContext) -> Recipe:
        # A single-dish swap doesn't need web grounding — skip it so revisions stay fast
        # (grounded calls are ~3x slower; the revise loop can fire several times per plan).
        with telemetry.record("replace", "gemini", self.model, grounded=False):
            return _generate_with_retry(
                lambda turns: self._call(turns, grounded=False),
                _replace_prompt(declined, ctx), _parse_recipe, self.max_attempts,
            )

    def _call(self, turns: list[tuple[str, str]], grounded: bool = True) -> str:
        contents = [
            {"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
            for role, text in turns
        ]
        system = _GROUNDED_SYSTEM if grounded else _SYSTEM
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
        }
        if grounded:
            body["tools"] = [{"google_search": {}}]

        def call() -> str:
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
            return "".join(part.get("text", "") for part in cand["content"]["parts"])

        return with_retry(call)
