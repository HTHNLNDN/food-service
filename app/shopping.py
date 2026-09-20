"""Shopping-list slice: aggregate a plan's ingredients (with amounts to buy), match each to
the best current offer among the SELECTED stores, and total the matched prices.

Recipe ingredients are English while Danish promo names don't token-match them
("chicken breast" != "Kyllingebryst", and "red" is a substring of "ørred"/trout), so matching
is delegated to a semantic OfferMatcher (an LLM). It only *picks which offer* — never a price;
prices come straight from the offer data. Picks are cached per ISO-week so page renders don't
re-call the model. Ingredients with no offer are listed explicitly with their amount, not dropped.
"""

import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

import httpx

from app import telemetry
from app.history import _norm_ing
from app.llm import chat_completion, extract_json_object
from app.offers import Offer
from app.planner import PlannedRecipe
from app.sections import SECTION_DISPLAY, section_for


def _human_amount(grams: float) -> str:
    g = round(grams)
    if g >= 1000:
        kg = g / 1000
        return f"{kg:.0f} kg" if kg == int(kg) else f"{kg:.1f} kg"
    return f"{g} g"


@dataclass(frozen=True)
class ShoppingItem:
    ingredient: str        # English — canonical identity (checklist key, offer matching)
    grams: float
    offer_name: str
    price: Decimal
    chain_slug: str
    display_name: str = ""  # translated label to print; falls back to `ingredient` when unset

    @property
    def amount(self) -> str:
        return _human_amount(self.grams)

    @property
    def label(self) -> str:
        return self.display_name or self.ingredient

    @property
    def section(self) -> str:
        return SECTION_DISPLAY[section_for(self.ingredient)]


@dataclass(frozen=True)
class UnmatchedItem:
    ingredient: str
    grams: float
    display_name: str = ""

    @property
    def amount(self) -> str:
        return _human_amount(self.grams)

    @property
    def label(self) -> str:
        return self.display_name or self.ingredient

    @property
    def section(self) -> str:
        return SECTION_DISPLAY[section_for(self.ingredient)]


@dataclass(frozen=True)
class ShoppingList:
    items: list[ShoppingItem]
    unmatched: list[UnmatchedItem]
    total: Decimal
    already_have: list[UnmatchedItem] = field(default_factory=list)  # user's use-up items, not bought

    def by_store_and_section(self) -> dict[str, dict[str, list[ShoppingItem]]]:
        by_store: dict[str, list[ShoppingItem]] = {}
        for item in self.items:
            by_store.setdefault(item.chain_slug, []).append(item)
        section_order = list(SECTION_DISPLAY.values())
        result: dict[str, dict[str, list[ShoppingItem]]] = {}
        for store, store_items in by_store.items():
            by_section: dict[str, list[ShoppingItem]] = {}
            for item in store_items:
                by_section.setdefault(item.section, []).append(item)
            # rebuild in SECTIONS order so Jinja iterates the intended walk order, not
            # insertion order — a dict comprehension over section_order, keeping only
            # sections that actually have items for this store
            result[store] = {s: by_section[s] for s in section_order if s in by_section}
        return result

    def by_section(self) -> dict[str, list[UnmatchedItem]]:
        by_section: dict[str, list[UnmatchedItem]] = {}
        for item in self.unmatched:
            by_section.setdefault(item.section, []).append(item)
        section_order = list(SECTION_DISPLAY.values())
        return {s: by_section[s] for s in section_order if s in by_section}


class OfferMatcher(Protocol):
    def match(self, ingredients: list[str], offers: list[Offer]) -> dict[str, int | None]:
        """Map each ingredient to the index of its best offer in `offers`, or None if none fit."""
        ...


# (offer_name, price, chain_slug) for a match, or None for a confirmed miss.
Match = tuple[str, Decimal, str]


def _aggregate(recipes: list[PlannedRecipe]) -> dict[str, float]:
    """Total grams per ingredient across the week's recipes (insertion order preserved)."""
    totals: dict[str, float] = {}
    for recipe in recipes:
        for ing in recipe.ingredients:
            totals[ing.name] = totals.get(ing.name, 0.0) + ing.grams
    return totals


def _ingredient_translations(recipes: list[PlannedRecipe]) -> dict[str, str]:
    """Merge each recipe's English-name -> translated-name map into one week-level lookup, so
    the same ingredient shows the same translated label everywhere on the shopping list."""
    merged: dict[str, str] = {}
    for recipe in recipes:
        if recipe.ingredient_translations:
            merged.update(recipe.ingredient_translations)
    return merged


# --- "already have" (use-up) matching: best-effort word overlap, like the app's other matchers ---

_UNITS = frozenset({"kg", "g", "l", "ml", "dl", "x", "stk", "pcs", "pc", "pack", "packs",
                    "tin", "tins", "can", "cans", "bag", "bags", "box"})


def _have_word_sets(have: str) -> list[frozenset[str]]:
    """Parse the free-text use-up list into significant-word sets, one per comma/line term."""
    sets: list[frozenset[str]] = []
    for term in re.split(r"[,\n]", have):
        words = frozenset(w for w in _norm_ing(term).split() if w not in _UNITS)
        if words:
            sets.append(words)
    return sets


def _is_have(name: str, have_sets: list[frozenset[str]]) -> bool:
    words = set(_norm_ing(name).split())
    return any(hs & words for hs in have_sets)


OFFER_MATCH_CAP = 150  # cap offers handed to the matcher — money is lowest priority; keeps the prompt small
_NAME_CAP = 60         # truncate long "X eller Y eller Z" promo names in the matcher prompt


def _cap_offers(offers: list[Offer]) -> list[Offer]:
    """Dedupe (chain, name), then round-robin across chains up to OFFER_MATCH_CAP — so a big
    multi-store catalog (8 stores x 100) doesn't blow up the matcher prompt, while staying
    balanced across the stores rather than favouring whichever came first."""
    seen: set[tuple[str, str]] = set()
    by_chain: dict[str, list[Offer]] = {}
    for o in offers:
        key = (o.chain_slug, o.name.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        by_chain.setdefault(o.chain_slug, []).append(o)
    out: list[Offer] = []
    i = 0
    while len(out) < OFFER_MATCH_CAP:
        added = False
        for lst in by_chain.values():
            if i < len(lst):
                out.append(lst[i])
                added = True
                if len(out) >= OFFER_MATCH_CAP:
                    break
        if not added:
            break
        i += 1
    return out


def build_shopping_list(
    recipes: list[PlannedRecipe],
    offers: list[Offer],
    selected: list[str],
    *,
    matcher: OfferMatcher | None = None,
    cache: sqlite3.Connection | None = None,
    week: str = "",
    have: str = "",
) -> ShoppingList:
    allowed = set(selected)
    in_scope = _cap_offers([o for o in offers if o.chain_slug in allowed])  # store whitelist + cap
    translations = _ingredient_translations(recipes)

    have_sets = _have_word_sets(have)
    aggregated = _aggregate(recipes)
    already_have = [UnmatchedItem(n, g, translations.get(n, ""))
                    for n, g in aggregated.items() if _is_have(n, have_sets)]
    amounts = {n: g for n, g in aggregated.items() if not _is_have(n, have_sets)}  # things to buy

    resolved: dict[str, Match | None] = {}
    misses: list[str] = []
    for name in amounts:
        cached = _cache_get(cache, week, name)
        if cached is _MISS:
            misses.append(name)
        else:
            resolved[name] = cached  # Match or None (negative)

    if misses and matcher is not None and in_scope:
        picks = matcher.match(misses, in_scope)
        for name in misses:
            idx = picks.get(name)
            offer = in_scope[idx] if isinstance(idx, int) and 0 <= idx < len(in_scope) else None
            match = (offer.name, offer.price, offer.chain_slug) if offer else None
            resolved[name] = match
            _cache_put(cache, week, name, match)
    else:
        for name in misses:
            resolved[name] = None  # no matcher available -> honest miss (not cached)

    items: list[ShoppingItem] = []
    unmatched: list[UnmatchedItem] = []
    for name, grams in amounts.items():
        match = resolved.get(name)
        if match is not None and match[2] in allowed:  # re-check store: selection may have changed
            items.append(ShoppingItem(name, grams, match[0], match[1], match[2], translations.get(name, "")))
        else:
            unmatched.append(UnmatchedItem(name, grams, translations.get(name, "")))

    total = sum((i.price for i in items), Decimal(0))
    return ShoppingList(items=items, unmatched=unmatched, total=total, already_have=already_have)


# --- per-week pick cache ---

_MISS = object()


def _cache_get(cache: sqlite3.Connection | None, week: str, name: str) -> object:
    if cache is None:
        return _MISS
    row = cache.execute(
        "SELECT offer_name, price, chain_slug FROM offer_match_cache WHERE week = ? AND name = ?",
        (week, name.strip().lower()),
    ).fetchone()
    if row is None:
        return _MISS
    if row["offer_name"] is None:
        return None  # negative cache: no offer matched this week
    return (row["offer_name"], Decimal(row["price"]), row["chain_slug"])


def _cache_put(cache: sqlite3.Connection | None, week: str, name: str, match: Match | None) -> None:
    if cache is None:
        return
    args = (week, name.strip().lower(), *((match[0], str(match[1]), match[2]) if match else (None, None, None)))
    cache.execute(
        "INSERT OR REPLACE INTO offer_match_cache(week, name, offer_name, price, chain_slug) "
        "VALUES (?, ?, ?, ?, ?)",
        args,
    )
    cache.commit()


# --- LLM matcher ---


def _as_index(val: object, n_offers: int) -> int | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if 0 <= val < n_offers else None
    if isinstance(val, str):
        m = re.search(r"\d+", val)
        if m:
            i = int(m.group())
            return i if 0 <= i < n_offers else None
    return None


class LLMOfferMatcher:
    """Matches English recipe ingredients to Danish grocery offers via any OpenAI-compatible
    chat model, in one batched call. The model only chooses *which offer* — never a price."""

    _SYSTEM = (
        "You match recipe ingredients to the best current grocery offer. The ingredients are in "
        "English; the offers are Danish supermarket promo names, so match by the actual food, "
        "not by spelling — e.g. 'chicken breast' -> 'Kyllingebryst', 'bell pepper' -> "
        "'Peberfrugt', 'salmon' -> 'Laks'. NEVER match on a coincidental substring ('red' is not "
        "'ørred'/trout; 'bell' is not 'kettlebell'; 'steak' of 'flanksteak' is pork, not veal). "
        "Skip offers that are not food or not that ingredient. Prefer the cheapest suitable offer. "
        "Reply with ONLY a JSON object mapping each ingredient (verbatim) to the offer's number, "
        "or null when no offer fits."
    )

    def __init__(self, base_url: str, model: str, api_key: str, client: httpx.Client | None = None):
        self.model = model
        self._client = client or httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=60
        )

    def match(self, ingredients: list[str], offers: list[Offer]) -> dict[str, int | None]:
        if not ingredients or not offers:
            return {name: None for name in ingredients}
        offer_lines = "\n".join(f"{i}: {o.name[:_NAME_CAP]} ({o.price} {o.currency})"
                                for i, o in enumerate(offers))
        ing_lines = "\n".join(f"- {name}" for name in ingredients)
        messages = [
            {"role": "system", "content": self._SYSTEM},
            {"role": "user", "content": f"Ingredients:\n{ing_lines}\n\nOffers:\n{offer_lines}\n\nJSON:"},
        ]
        with telemetry.record("offer_match", "openai-compat", self.model):
            content = chat_completion(self._client, self.model, messages)
        obj = extract_json_object(content)
        lower = {str(k).lower(): v for k, v in obj.items()}
        n = len(offers)
        return {name: _as_index(obj.get(name, lower.get(name.lower())), n) for name in ingredients}
