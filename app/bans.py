"""Food bans: curated categories + custom items, with deterministic ingredient matching.

Bans are stored as a canonical list of strings — category ids (``fish``, ``dairy`` …) plus
custom item strings (lowercased). Categories expand, via the curated map below, to (a)
display names for the prompt and (b) ingredient keywords for the deterministic guard in the
planner. Matching is best-effort word-boundary matching — not a guarantee for severe
allergies (surfaced in the UI).
"""

import re

# base keyword sets (lowercase ingredient words)
_KW = {
    "fish": ["fish", "salmon", "cod", "tuna", "trout", "herring", "mackerel", "sardine",
             "anchovy", "haddock", "pollock", "tilapia", "plaice"],
    "shellfish": ["shellfish", "shrimp", "prawn", "prawns", "crab", "lobster", "mussel",
                  "mussels", "oyster", "clam", "scallop", "scallops", "squid"],
    "pork": ["pork", "bacon", "ham", "prosciutto", "chorizo", "salami"],
    "beef": ["beef", "veal", "steak"],
    "poultry": ["chicken", "turkey", "duck", "poultry"],
    "lamb": ["lamb", "mutton"],
    "dairy": ["milk", "cheese", "butter", "cream", "yogurt", "yoghurt", "dairy", "lactose",
              "mozzarella", "feta", "parmesan", "cheddar", "skyr"],
    "egg": ["egg", "eggs"],
    "gluten": ["wheat", "bread", "pasta", "flour", "gluten", "couscous", "barley",
               "breadcrumb", "breadcrumbs", "noodle", "noodles"],
    "nuts": ["almond", "almonds", "walnut", "walnuts", "peanut", "peanuts", "cashew",
             "cashews", "hazelnut", "pistachio", "pecan", "nut", "nuts"],
    "soy": ["soy", "soya", "tofu", "edamame", "tempeh"],
}
_MEAT = _KW["beef"] + _KW["pork"] + _KW["poultry"] + _KW["lamb"] + ["meat", "sausage", "mince"]
_VEGETARIAN = _MEAT + _KW["fish"] + _KW["shellfish"]
_VEGAN = _VEGETARIAN + _KW["dairy"] + _KW["egg"]

# Plant-based "milk/butter/cream/cheese" (coconut milk, peanut butter, vegan cheese…) are NOT
# dairy — a preceding plant qualifier exempts the dairy keywords (but not a nut/soy ban: "peanut"
# still trips the nuts ban).
_PLANT_QUALIFIERS = {"coconut", "almond", "soy", "soya", "oat", "cashew", "rice", "peanut",
                     "hemp", "hazelnut", "macadamia", "walnut", "sunflower", "plant", "vegan", "pea"}
_DAIRY_SENSITIVE = {"milk", "butter", "cream", "cheese", "yogurt", "yoghurt"}

# (id, display, keywords) — order is the UI chip order.
BAN_CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("fish", "Fish", _KW["fish"]),
    ("shellfish", "Shellfish", _KW["shellfish"]),
    ("pork", "Pork", _KW["pork"]),
    ("beef", "Beef & veal", _KW["beef"]),
    ("poultry", "Poultry", _KW["poultry"]),
    ("meat", "All meat", _MEAT),
    ("dairy", "Dairy & lactose", _KW["dairy"]),
    ("egg", "Egg", _KW["egg"]),
    ("gluten", "Gluten", _KW["gluten"]),
    ("nuts", "Nuts", _KW["nuts"]),
    ("soy", "Soy", _KW["soy"]),
    ("vegetarian", "Vegetarian (no meat or fish)", _VEGETARIAN),
    ("vegan", "Vegan (no animal products)", _VEGAN),
]
_CATEGORY = {cid: (display, kws) for cid, display, kws in BAN_CATEGORIES}
KNOWN_CATEGORY_IDS = set(_CATEGORY)


def ban_display(bans: list[str]) -> list[str]:
    """Human-readable ban names for the prompt: category ids -> display, custom kept verbatim."""
    return [_CATEGORY[b][0] if b in _CATEGORY else b for b in bans]


def _keywords(bans: list[str]) -> set[str]:
    words: set[str] = set()
    for b in bans:
        if b in _CATEGORY:
            words.update(_CATEGORY[b][1])
        elif b.strip():
            words.add(b.strip().lower())  # custom item is its own keyword
    return words


def _has_plant_qualifier(low: str) -> bool:
    return any(re.search(rf"\b{q}\b", low) for q in _PLANT_QUALIFIERS)


def banned_hits(ingredient_names: list[str], bans: list[str]) -> set[str]:
    """Which banned terms appear (as whole words) in the ingredient names. A plant-based
    milk/butter/cream/cheese does not count as a dairy hit."""
    words = _keywords(bans)
    hits: set[str] = set()
    for name in ingredient_names:
        low = name.lower()
        plant = _has_plant_qualifier(low)
        for w in words:
            if not re.search(rf"\b{re.escape(w)}\b", low):
                continue
            if plant and w in _DAIRY_SENSITIVE:
                continue  # e.g. "coconut milk" / "peanut butter" is not dairy
            hits.add(w)
    return hits


def violates(ingredients, bans: list[str]) -> bool:
    return bool(banned_hits([i.name for i in ingredients], bans))
