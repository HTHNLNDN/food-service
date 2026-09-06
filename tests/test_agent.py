import json

import httpx
import pytest

from app.agent import (
    GeminiGroundedAgent,
    OpenAICompatibleAgent,
    PlanContext,
    Recipe,
    RecipeError,
)

LLM_BASE = "https://llm.test/v1"

CTX = PlanContext(
    max_kcal=600,
    min_protein_g=40,
    restrictions="PCOS",
    servings=4,
    num_dinners=2,
    offers=["Kyllingebryst", "Broccoli"],
    preference_summary="",
)


def _recipe_obj(title):
    return {
        "title": title,
        "servings": 4,
        "ingredients": [
            {"name": "chicken breast", "grams": 600},
            {"name": "broccoli", "grams": 400},
        ],
        "steps": ["Grill the chicken.", "Steam the broccoli."],
    }


def _completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _agent(handler):
    client = httpx.Client(base_url=LLM_BASE, transport=httpx.MockTransport(handler))
    return OpenAICompatibleAgent(base_url=LLM_BASE, model="test-model", api_key="k", client=client)


def test_plan_week_parses_recipes_with_gram_ingredients():
    body = json.dumps({"recipes": [_recipe_obj("A"), _recipe_obj("B")]})
    agent = _agent(lambda _req: _completion(body))
    recipes = agent.plan_week(CTX)
    assert [r.title for r in recipes] == ["A", "B"]
    assert all(isinstance(r, Recipe) for r in recipes)
    first = recipes[0].ingredients[0]
    assert first.name == "chicken breast"
    assert first.grams == 600.0  # numeric grams, usable by nutrition.macros_for


def test_plan_week_retries_on_invalid_then_succeeds():
    state = {"n": 0}

    def handler(_req):
        state["n"] += 1
        if state["n"] == 1:
            return _completion("sorry, here is a recipe in prose, no JSON")
        return _completion(json.dumps({"recipes": [_recipe_obj("A"), _recipe_obj("B")]}))

    agent = _agent(handler)
    recipes = agent.plan_week(CTX)
    assert len(recipes) == 2
    assert state["n"] == 2  # one retry


def test_plan_week_raises_after_exhausting_retries():
    state = {"n": 0}

    def handler(_req):
        state["n"] += 1
        return _completion("never valid json")

    agent = _agent(handler)
    agent.max_attempts = 3
    with pytest.raises(RecipeError):
        agent.plan_week(CTX)
    assert state["n"] == 3


def test_plan_week_tolerates_markdown_code_fences():
    fenced = "```json\n" + json.dumps({"recipes": [_recipe_obj("A"), _recipe_obj("B")]}) + "\n```"
    agent = _agent(lambda _req: _completion(fenced))
    assert len(agent.plan_week(CTX)) == 2


def test_replace_returns_single_recipe():
    declined = Recipe(title="Old", servings=4, ingredients=[], steps=[])
    agent = _agent(lambda _req: _completion(json.dumps(_recipe_obj("New"))))
    result = agent.replace(declined, CTX)
    assert isinstance(result, Recipe)
    assert result.title == "New"


def test_system_prompt_prioritises_simple_quick_recipes_over_offers():
    from app.agent import _SYSTEM

    s = _SYSTEM.lower()
    assert "simple" in s          # simple dinners
    assert "30 min" in s          # ~30 minutes, not an hour-long project
    assert "opportun" in s        # offers used opportunistically, not as a requirement


def test_system_prompt_optimises_for_fibre_softly():
    from app.agent import _SYSTEM

    s = _SYSTEM.lower()
    assert "fibre" in s
    assert "not a hard requirement" in s  # optimiser, not a gate


def test_constraints_include_hard_ban_instruction():
    from app.agent import _constraints

    ctx = PlanContext(max_kcal=600, min_protein_g=40, restrictions="", servings=4,
                      num_dinners=2, offers=[], preference_summary="", bans=["fish", "mushroom"])
    text = _constraints(ctx)
    assert "NEVER include" in text
    assert "Fish" in text        # category -> display name
    assert "mushroom" in text    # custom item verbatim


def test_system_prompt_is_price_conscious_without_banning_beef():
    from app.agent import _SYSTEM

    s = _SYSTEM.lower()
    assert "pork" in s and "chicken" in s     # cheap everyday DK proteins encouraged
    assert "beef" in s and "veal" in s        # still allowed...
    assert "occasional" in s                  # ...as an occasional choice, not discouraged/avoided


def test_constraints_include_use_up_ingredients():
    from app.agent import _constraints

    ctx = PlanContext(max_kcal=600, min_protein_g=40, restrictions="", servings=4,
                      num_dinners=2, offers=[], preference_summary="", use_up="3 kg chicken breast")
    text = _constraints(ctx)
    assert "use them up" in text
    assert "3 kg chicken breast" in text


def test_constraints_include_avoid_recent_dinners():
    from app.agent import _constraints

    ctx = PlanContext(max_kcal=600, min_protein_g=40, restrictions="", servings=4,
                      num_dinners=2, offers=[], preference_summary="",
                      avoid_dishes=["Thai Basil Chicken", "Creamy Tuscan Chicken"])
    text = _constraints(ctx)
    assert "Do NOT repeat" in text
    assert "Thai Basil Chicken" in text
    assert "Creamy Tuscan Chicken" in text


def test_system_prompt_asks_for_variety():
    from app.agent import _SYSTEM

    s = _SYSTEM.lower()
    assert "vary" in s          # explicit variety directive (the boring-recipes fix)
    assert "protein" in s and ("cooking method" in s or "technique" in s)


def test_offers_are_framed_as_optional_deals():
    from app.agent import _plan_prompt

    text = _plan_prompt(CTX).lower()  # CTX has offers
    assert "optional" in text         # deals presented as optional, not mandatory


def test_request_targets_openai_chat_completions_with_model():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["model"] = json.loads(req.content)["model"]
        return _completion(json.dumps({"recipes": [_recipe_obj("A"), _recipe_obj("B")]}))

    _agent(handler).plan_week(CTX)
    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["model"] == "test-model"


# --- GeminiGroundedAgent (native generateContent + google_search, mocked) ---

GEN_BASE = "https://gen.test/v1beta"


def _gen_response(text):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def _grounded(handler):
    client = httpx.Client(base_url=GEN_BASE, transport=httpx.MockTransport(handler))
    return GeminiGroundedAgent(GEN_BASE, "gemini-x", "k", client=client)


def test_grounded_plan_week_parses_recipes():
    body = json.dumps({"recipes": [_recipe_obj("A"), _recipe_obj("B")]})
    recipes = _grounded(lambda _r: _gen_response(body)).plan_week(CTX)
    assert [r.title for r in recipes] == ["A", "B"]


def test_grounded_request_uses_generatecontent_with_search_tool():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return _gen_response(json.dumps({"recipes": [_recipe_obj("A"), _recipe_obj("B")]}))

    _grounded(handler).plan_week(CTX)
    assert seen["url"].endswith("/models/gemini-x:generateContent")
    assert seen["body"]["tools"] == [{"google_search": {}}]
    assert "systemInstruction" in seen["body"]


def test_grounded_retries_on_invalid_then_succeeds():
    state = {"n": 0}

    def handler(_req):
        state["n"] += 1
        if state["n"] == 1:
            return _gen_response("sorry, no JSON")
        return _gen_response(json.dumps({"recipes": [_recipe_obj("A"), _recipe_obj("B")]}))

    assert len(_grounded(handler).plan_week(CTX)) == 2
    assert state["n"] == 2


def test_grounded_extracts_json_wrapped_in_prose():
    wrapped = "Based on sources [1][2]:\n" + json.dumps({"recipes": [_recipe_obj("A"), _recipe_obj("B")]}) + "\nSources: example.com"
    assert len(_grounded(lambda _r: _gen_response(wrapped)).plan_week(CTX)) == 2


def test_grounded_replace_returns_single_recipe():
    result = _grounded(lambda _r: _gen_response(json.dumps(_recipe_obj("New")))).replace(
        Recipe("Old", 4, [], []), CTX
    )
    assert result.title == "New"


def test_grounded_replace_skips_search_grounding_for_speed():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return _gen_response(json.dumps(_recipe_obj("New")))

    _grounded(handler).replace(Recipe("Old", 4, [], []), CTX)
    assert "tools" not in seen["body"]  # single-dish swaps skip google_search (grounded is ~3x slower)
