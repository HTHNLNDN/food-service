"""Grocery-store section categorization for the shopping list: deterministic keyword matching,
mirroring app/bans.py's existing curated-category pattern exactly — no LLM, no cache, since this
is a small, well-known taxonomy that never varies. Sections are ordered to match a typical
supermarket walk (fresh perimeter first, frozen near the end).

Ingredient names are always matched in English (the canonical key already used for offer
matching and translation lookups), never a translated display label.
"""

import re

# Checked FIRST, in order, before the per-section keyword loop below. Each entry overrides
# whatever base-ingredient word also appears in the name — "garlic powder" is pantry, not
# produce; "coconut milk" is pantry, not dairy (same plant-qualifier problem app/bans.py solves
# for dietary bans); "frozen peas" is frozen, not produce.
_OVERRIDES: list[tuple[str, list[str]]] = [
    ("frozen", ["frozen"]),
    ("pantry", [
        "dried", "canned", "crushed canned", "tinned", "tin of",       # canned/dried goods
        "broth", "stock", "oil", "vinegar", "powder", "paste", "passata", "sauce", "juice",
        "coconut milk", "coconut cream", "almond milk", "oat milk", "soy milk", "cashew milk",
    ]),
]

# (id, display, keywords) — order is the shopping-walk order.
SECTIONS: list[tuple[str, str, list[str]]] = [
    ("produce", "Fruit & vegetables", [
        "apple", "asparagus", "avocado", "bell pepper", "broccoli",
        "brussels sprout", "brussels sprouts", "cabbage", "carrot", "carrots",
        "cauliflower", "celery", "cherry tomato", "cherry tomatoes", "cucumber",
        "eggplant", "fresh basil", "fresh cilantro", "fresh coriander",
        "coriander leaves", "fresh dill", "dill", "fresh ginger", "ginger",
        "fresh parsley", "garlic", "green bean", "green beans", "green onion",
        "green onions", "spring onion", "butter lettuce", "iceberg lettuce",
        "romaine lettuce", "lettuce", "jalapeño pepper", "jalapeno pepper",
        "red chili pepper", "chili pepper", "lemongrass", "mushroom", "mushrooms",
        "onion", "potato", "potatoes", "red cabbage", "shallot", "snow pea",
        "snow peas", "sugar snap pea", "sugar snap peas", "spinach", "sweet corn",
        "sweet potato", "thai basil", "tomato", "zucchini", "kalamata olive",
        "kalamata olives",
    ]),
    ("bakery", "Bakery", [
        "bread", "pita", "baguette", "bun", "roll", "tortilla", "naan",
    ]),
    ("meat_fish", "Meat, poultry & fish", [
        "beef", "steak", "pork", "chop", "tenderloin", "ham", "chicken", "turkey",
        "veal", "lamb", "bacon", "sausage", "mince", "minced", "salmon fillet",
        "haddock fillet", "trout fillet", "shrimp",
    ]),
    ("dairy_eggs", "Dairy & eggs", [
        "butter", "cheese", "cream", "yogurt", "yoghurt", "skyr", "egg", "eggs",
        "milk", "feta", "mozzarella", "parmesan", "grana padano",
    ]),
    ("pantry", "Pantry & dry goods", [
        "rice", "pasta", "quinoa", "flour", "sugar", "honey", "salt",
        "black pepper", "red pepper flakes", "paprika", "cumin", "coriander",
        "turmeric", "cinnamon", "seasoning", "spice", "mustard", "mirin",
        "cornstarch", "bean", "chickpea", "lentil", "olive", "olives", "caper",
        "capers", "caraway", "hummus", "salsa", "pesto", "tofu", "edamame",
        "kimchi", "sun-dried", "sun dried", "curry powder", "garam masala",
        "sriracha", "worcestershire", "sesame seed", "sesame seeds", "oregano",
        "thyme",
    ]),
    ("frozen", "Frozen", []),  # populated only via the frozen override above
    ("beverages", "Beverages", [
        "water",
    ]),
    ("other", "Other", []),  # catch-all — always matches, must be last
]

# id -> display name, derived once so callers doing repeated lookups (ShoppingItem.section
# and friends, Task 2) don't linear-scan SECTIONS on every access.
SECTION_DISPLAY: dict[str, str] = {sid: display for sid, display, _kw in SECTIONS}


def section_for(ingredient_name: str) -> str:
    """Word-boundary keyword match against `ingredient_name`, returning a section id from
    SECTIONS. Falls back to "other" when nothing matches."""
    low = ingredient_name.lower()
    for section_id, qualifiers in _OVERRIDES:
        for q in qualifiers:
            if re.search(rf"\b{re.escape(q)}\b", low):
                return section_id
    for section_id, _display, keywords in SECTIONS:
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", low):
                return section_id
    return "other"
