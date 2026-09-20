import json
import sqlite3
from decimal import Decimal

import httpx

from app.db import bootstrap
from app.nutrition import Ingredient, Macros
from app.offers import Offer
from app.planner import PlannedRecipe
from app.shopping import (
    LLMOfferMatcher,
    ShoppingItem,
    ShoppingList,
    UnmatchedItem,
    _human_amount,
    build_shopping_list,
)

ZERO = Macros(0, 0, 0, 0)


def _recipe(*ingredients, grams=100):
    """ingredients: names, or (name, grams) tuples."""
    ings = [
        Ingredient(name=i[0], grams=i[1]) if isinstance(i, tuple) else Ingredient(name=i, grams=grams)
        for i in ingredients
    ]
    return PlannedRecipe(title="r", servings=4, ingredients=ings, steps=[], per_serving=ZERO, flagged=False)


def _offer(slug, name, price):
    return Offer(
        chain_slug=slug, name=name, price=Decimal(str(price)), currency="DKK",
        valid_from="", valid_to="", unit_price=None, volume_ml=None, unit_count=None, price_kind="campaign",
    )


class FakeMatcher:
    """Deterministic stand-in: maps ingredient -> offer index (or None). Records its calls."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list[list[str]] = []

    def match(self, ingredients, offers):
        self.calls.append(list(ingredients))
        return {name: self.mapping.get(name) for name in ingredients}


def _cache():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap(conn)
    return conn


# --- matching + pricing ---


def test_matcher_pick_flows_through_with_price_and_total():
    recipes = [_recipe("broccoli")]
    offers = [_offer("rema", "Broccoli", 10), _offer("rema", "Frisk broccoli", 8)]
    matcher = FakeMatcher({"broccoli": 1})  # ranking is the matcher's job now
    sl = build_shopping_list(recipes, offers, ["rema"], matcher=matcher)
    assert len(sl.items) == 1
    assert sl.items[0].price == Decimal(8)
    assert sl.items[0].chain_slug == "rema"
    assert sl.total == Decimal(8)


def test_strict_store_filter_hides_non_selected_chains_from_matcher():
    recipes = [_recipe("broccoli")]
    offers = [_offer("rema", "Broccoli", 10), _offer("netto", "Broccoli", 5)]  # netto not selected
    matcher = FakeMatcher({"broccoli": 0})
    sl = build_shopping_list(recipes, offers, ["rema"], matcher=matcher)
    assert matcher.calls == [["broccoli"]]
    assert sl.items[0].chain_slug == "rema"
    assert sl.items[0].price == Decimal(10)  # only the rema offer was in scope


def test_unmatched_ingredient_is_listed_with_amount():
    recipes = [_recipe(("quinoa", 250))]
    offers = [_offer("rema", "Broccoli", 10)]
    matcher = FakeMatcher({"quinoa": None})
    sl = build_shopping_list(recipes, offers, ["rema"], matcher=matcher)
    assert sl.items == []
    assert [(u.ingredient, u.amount) for u in sl.unmatched] == [("quinoa", "250 g")]
    assert sl.total == Decimal(0)


def test_dedupes_and_sums_amounts_across_recipes():
    recipes = [_recipe(("chicken breast", 400)), _recipe(("chicken breast", 300), ("broccoli", 200))]
    offers = [_offer("rema", "Kyllingebryst", 20), _offer("rema", "Broccoli", 8)]
    matcher = FakeMatcher({"chicken breast": 0, "broccoli": 1})
    sl = build_shopping_list(recipes, offers, ["rema"], matcher=matcher)
    by_name = {i.ingredient: i for i in sl.items}
    assert set(by_name) == {"chicken breast", "broccoli"}  # chicken appears once
    assert by_name["chicken breast"].grams == 700  # 400 + 300 summed
    assert by_name["chicken breast"].amount == "700 g"
    assert sl.total == Decimal(28)


def test_by_store_groups_items():
    recipes = [_recipe("broccoli", "chicken breast")]
    offers = [_offer("rema", "Broccoli", 8), _offer("foetex", "Kyllingebryst", 20)]
    matcher = FakeMatcher({"broccoli": 0, "chicken breast": 1})
    sl = build_shopping_list(recipes, offers, ["rema", "foetex"], matcher=matcher)
    grouped = sl.by_store_and_section()
    assert set(grouped) == {"rema", "foetex"}


def test_no_matcher_leaves_everything_unmatched():
    recipes = [_recipe("broccoli")]
    offers = [_offer("rema", "Broccoli", 8)]
    sl = build_shopping_list(recipes, offers, ["rema"])  # matcher=None
    assert sl.items == []
    assert [u.ingredient for u in sl.unmatched] == ["broccoli"]


# --- "already have" / use-up exclusion ---


def test_have_excludes_use_up_ingredients_from_buying():
    recipes = [_recipe(("chicken breast", 1600), ("broccoli florets", 500), ("rice", 400))]
    offers = [_offer("rema", "Broccoli", 8), _offer("rema", "Ris", 12)]
    matcher = FakeMatcher({"rice": 1})
    sl = build_shopping_list(recipes, offers, ["rema"], matcher=matcher,
                             have="3kg chicken breast, freezer broccoli")
    assert {u.ingredient for u in sl.already_have} == {"chicken breast", "broccoli florets"}
    assert [i.ingredient for i in sl.items] == ["rice"]        # only the un-owned thing is bought
    assert matcher.calls == [["rice"]]                          # matcher not asked about owned items
    buy_names = {i.ingredient for i in sl.items} | {u.ingredient for u in sl.unmatched}
    assert "chicken breast" not in buy_names and "broccoli florets" not in buy_names


def test_already_have_carries_amount():
    sl = build_shopping_list([_recipe(("chicken breast", 1600))], [], ["rema"], have="chicken breast")
    assert sl.already_have[0].amount == "1.6 kg"


def test_have_empty_is_a_noop():
    matcher = FakeMatcher({"broccoli": 0})
    sl = build_shopping_list([_recipe("broccoli")], [_offer("rema", "Broccoli", 8)], ["rema"], matcher=matcher)
    assert sl.already_have == []
    assert [i.ingredient for i in sl.items] == ["broccoli"]


# --- amount formatting ---


# --- display translation (English `ingredient` stays the key; `label` shows the translation) ---


def test_matched_item_shows_translated_label_but_keeps_english_ingredient_key():
    recipe = PlannedRecipe(
        title="r", servings=4, ingredients=[Ingredient("chicken breast", 400)], steps=[],
        per_serving=ZERO, flagged=False, ingredient_translations={"chicken breast": "kyllingebryst"},
    )
    matcher = FakeMatcher({"chicken breast": 0})
    sl = build_shopping_list([recipe], [_offer("rema", "Kyllingebryst", 20)], ["rema"], matcher=matcher)
    assert sl.items[0].ingredient == "chicken breast"   # unchanged lookup/checklist key
    assert sl.items[0].label == "kyllingebryst"          # what's printed


def test_unmatched_and_already_have_items_also_get_translated_labels():
    recipe = PlannedRecipe(
        title="r", servings=4, ingredients=[Ingredient("quinoa", 250)], steps=[],
        per_serving=ZERO, flagged=False, ingredient_translations={"quinoa": "quinoafrø"},
    )
    matcher = FakeMatcher({"quinoa": None})
    sl = build_shopping_list([recipe], [], ["rema"], matcher=matcher)
    assert sl.unmatched[0].label == "quinoafrø"

    sl2 = build_shopping_list([recipe], [], ["rema"], have="quinoa")
    assert sl2.already_have[0].label == "quinoafrø"


def test_label_falls_back_to_english_when_no_translation():
    sl = build_shopping_list([_recipe("broccoli")], [], ["rema"])
    assert sl.unmatched[0].label == "broccoli"


def test_human_amount_formats_grams_and_kilos():
    assert _human_amount(300) == "300 g"
    assert _human_amount(999) == "999 g"
    assert _human_amount(1000) == "1 kg"
    assert _human_amount(1500) == "1.5 kg"
    assert _human_amount(2000) == "2 kg"
    assert _human_amount(1400) == "1.4 kg"  # one-decimal kg


# --- per-week cache (renders must not re-call the LLM) ---


def test_cache_avoids_a_second_matcher_call_same_week():
    conn = _cache()
    recipes = [_recipe("broccoli"), _recipe(("quinoa", 250))]
    offers = [_offer("rema", "Broccoli", 8)]
    matcher = FakeMatcher({"broccoli": 0, "quinoa": None})

    first = build_shopping_list(recipes, offers, ["rema"], matcher=matcher, cache=conn, week="2026-W36")
    second = build_shopping_list(recipes, offers, ["rema"], matcher=matcher, cache=conn, week="2026-W36")

    assert len(matcher.calls) == 1  # second render served entirely from cache (incl. the negative)
    assert first.items[0].offer_name == second.items[0].offer_name == "Broccoli"
    assert [u.ingredient for u in second.unmatched] == ["quinoa"]


def test_cache_is_rematched_on_a_new_week():
    conn = _cache()
    recipes = [_recipe("broccoli")]
    offers = [_offer("rema", "Broccoli", 8)]
    matcher = FakeMatcher({"broccoli": 0})
    build_shopping_list(recipes, offers, ["rema"], matcher=matcher, cache=conn, week="2026-W36")
    build_shopping_list(recipes, offers, ["rema"], matcher=matcher, cache=conn, week="2026-W37")
    assert len(matcher.calls) == 2  # new week -> offers reset -> re-matched


# --- offer cap / dedupe / truncation (keep the matcher prompt small) ---


class RecordingMatcher:
    def __init__(self):
        self.offers_seen = None

    def match(self, ingredients, offers):
        self.offers_seen = list(offers)
        return {n: None for n in ingredients}


def test_offers_deduped_capped_and_balanced_across_stores():
    from app.shopping import OFFER_MATCH_CAP
    offers = ([_offer("rema", f"rema item {i}", 10) for i in range(100)]
              + [_offer("netto", f"netto item {i}", 10) for i in range(100)])
    m = RecordingMatcher()
    build_shopping_list([_recipe("chicken breast")], offers, ["rema", "netto"], matcher=m)
    assert len(m.offers_seen) <= OFFER_MATCH_CAP
    assert {o.chain_slug for o in m.offers_seen} == {"rema", "netto"}  # round-robin keeps both


def test_offers_deduped_by_name():
    offers = [_offer("rema", "Broccoli", 8)] * 3
    m = RecordingMatcher()
    build_shopping_list([_recipe("x")], offers, ["rema"], matcher=m)
    assert len(m.offers_seen) == 1


def test_matcher_prompt_truncates_long_names():
    seen = {}

    def handler(req):
        seen["content"] = json.loads(req.content)["messages"][-1]["content"]
        return _completion(json.dumps({"x": 0}))

    _matcher(handler).match(["x"], [_offer("rema", "A" * 200, 10)])
    assert "A" * 200 not in seen["content"]  # full 200-char name not sent
    assert "A" * 60 in seen["content"]        # truncated to the cap


# --- LLMOfferMatcher parsing (mocked chat endpoint) ---

BASE = "https://llm.test/v1"


def _completion(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _matcher(handler):
    client = httpx.Client(base_url=BASE, transport=httpx.MockTransport(handler))
    return LLMOfferMatcher(BASE, "m", "k", client=client)


def _offers(*names):
    return [_offer("rema", n, 10) for n in names]


def test_llm_matcher_parses_index_mapping():
    body = json.dumps({"chicken breast": 1, "bell pepper": 0})
    m = _matcher(lambda _r: _completion(body))
    result = m.match(["chicken breast", "bell pepper"], _offers("Peberfrugt", "Kyllingebryst"))
    assert result == {"chicken breast": 1, "bell pepper": 0}


def test_llm_matcher_null_and_out_of_range_become_none():
    body = json.dumps({"quinoa": None, "salt": 9})  # 9 is out of range
    m = _matcher(lambda _r: _completion(body))
    assert m.match(["quinoa", "salt"], _offers("Broccoli")) == {"quinoa": None, "salt": None}


def test_llm_matcher_tolerates_fenced_and_prose_json():
    body = "Here are the matches:\n```json\n" + json.dumps({"broccoli": 0}) + "\n```\nHope that helps."
    m = _matcher(lambda _r: _completion(body))
    assert m.match(["broccoli"], _offers("Broccoli")) == {"broccoli": 0}


def test_llm_matcher_is_case_insensitive_on_keys():
    body = json.dumps({"Chicken Breast": 0})
    m = _matcher(lambda _r: _completion(body))
    assert m.match(["chicken breast"], _offers("Kyllingebryst")) == {"chicken breast": 0}


def test_llm_matcher_short_circuits_with_no_offers():
    calls = {"n": 0}

    def handler(_r):
        calls["n"] += 1
        return _completion("{}")

    m = _matcher(handler)
    assert m.match(["broccoli"], []) == {"broccoli": None}
    assert calls["n"] == 0  # no offers -> no LLM call


# --- store-section grouping (app/sections.py) ---


def test_item_section_property_matches_section_for():
    item = ShoppingItem("chicken breast", 400, "Kylling", Decimal(20), "rema")
    assert item.section == "Meat, poultry & fish"
    unmatched = UnmatchedItem("broccoli", 300)
    assert unmatched.section == "Fruit & vegetables"


def test_by_store_and_section_groups_in_walk_order_per_store():
    items = [
        ShoppingItem("chicken breast", 400, "Kylling", Decimal(20), "rema"),
        ShoppingItem("broccoli", 300, "Broccoli", Decimal(10), "rema"),
        ShoppingItem("butter", 200, "Smør", Decimal(15), "netto"),
    ]
    lst = ShoppingList(items=items, unmatched=[], total=Decimal(45))
    grouped = lst.by_store_and_section()

    assert list(grouped.keys()) == ["rema", "netto"]  # store order = insertion order of items
    rema_sections = list(grouped["rema"].keys())
    # produce comes before meat_fish in SECTIONS order, regardless of item insertion order above
    assert rema_sections == ["Fruit & vegetables", "Meat, poultry & fish"]
    assert grouped["rema"]["Fruit & vegetables"] == [items[1]]
    assert grouped["rema"]["Meat, poultry & fish"] == [items[0]]
    assert grouped["netto"] == {"Dairy & eggs": [items[2]]}


def test_by_store_and_section_omits_empty_sections():
    items = [ShoppingItem("water", 1000, "Vand", Decimal(5), "rema")]
    lst = ShoppingList(items=items, unmatched=[], total=Decimal(5))
    grouped = lst.by_store_and_section()
    assert list(grouped["rema"].keys()) == ["Beverages"]  # only the one populated section


def test_by_section_groups_unmatched_items_in_walk_order():
    unmatched = [
        UnmatchedItem("rice", 500),
        UnmatchedItem("carrot", 200),
    ]
    lst = ShoppingList(items=[], unmatched=unmatched, total=Decimal(0))
    grouped = lst.by_section()
    # produce ("carrot") before pantry ("rice") in SECTIONS order, despite insertion order above
    assert list(grouped.keys()) == ["Fruit & vegetables", "Pantry & dry goods"]
    assert grouped["Fruit & vegetables"] == [unmatched[1]]
    assert grouped["Pantry & dry goods"] == [unmatched[0]]
