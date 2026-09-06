"""Deterministic scorers for the offline eval — they reuse the app's determinism wall, so they
cost nothing and have no sampling noise (reliable even at small N). Each takes the produced
PlannedRecipe list (+ the scenario where targets/bans matter) and returns a number."""

from itertools import combinations

from app.bans import violates
from app.history import _jaccard, _norm_ings

# Primary-protein classification by ingredient keyword (the headline variety signal from the audit).
_PROTEIN = {
    "chicken/poultry": ("chicken", "turkey", "poultry"),
    "pork": ("pork", "bacon", "ham", "sausage", "chorizo"),
    "beef/veal": ("beef", "veal", "steak", "mince"),
    "fish": ("salmon", "cod", "tuna", "trout", "herring", "mackerel", "haddock", "fish", "tilapia"),
    "shellfish": ("shrimp", "prawn", "scampi", "mussel", "squid", "crab", "lobster"),
    "egg": ("egg",),
    "legume/tofu": ("tofu", "tempeh", "lentil", "chickpea", "bean", "edamame"),
}


def _primary_protein(ingredients) -> str:
    best, best_g = "other", -1.0
    for ing in ingredients:
        name = ing.name.lower()
        for label, kws in _PROTEIN.items():
            if any(k in name for k in kws) and ing.grams > best_g:
                best, best_g = label, ing.grams
    return best


def macro_pass_rate(recipes, scenario) -> float:
    """Fraction of dinners strictly within the scenario's kcal ceiling and protein floor."""
    if not recipes:
        return 0.0
    ok = 0
    for r in recipes:
        within = ((scenario.max_kcal is None or r.per_serving.kcal <= scenario.max_kcal)
                  and (scenario.min_protein_g is None or r.per_serving.protein_g >= scenario.min_protein_g))
        ok += within
    return ok / len(recipes)


def ban_violations(recipes, scenario) -> int:
    """Dinners containing a banned ingredient — must be 0."""
    return sum(violates(r.ingredients, list(scenario.bans)) for r in recipes)


def flagged_rate(recipes) -> float:
    return sum(r.flagged for r in recipes) / len(recipes) if recipes else 0.0


def distinct_proteins(recipes) -> int:
    return len({_primary_protein(r.ingredients) for r in recipes})


def repetition(recipes) -> float:
    """Mean within-plan ingredient-set Jaccard (lower = more varied). Staples are ignored via
    history._norm_ings, so it reflects distinctive ingredients."""
    sets = [_norm_ings(i.name for i in r.ingredients) for r in recipes]
    pairs = list(combinations(sets, 2))
    return sum(_jaccard(a, b) for a, b in pairs) / len(pairs) if pairs else 0.0
