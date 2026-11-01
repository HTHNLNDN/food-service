"""Deterministic scorers for the offline eval — they reuse the app's determinism wall, so they
cost nothing and have no sampling noise (reliable even at small N). Each takes the produced
PlannedRecipe list (+ the scenario where targets/bans matter) and returns a number."""

import re
from itertools import combinations

from app.agent import KCAL_TOLERANCE
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
    """Fraction of dinners within 10% of the kcal target (either way) that meet the protein
    floor. The kcal preference is a target, not a ceiling."""
    if not recipes:
        return 0.0
    tol = KCAL_TOLERANCE
    ok = 0
    for r in recipes:
        kcal, target = r.per_serving.kcal, scenario.max_kcal
        within = ((target is None or target * (1 - tol) <= kcal <= target * (1 + tol))
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


def kcal_use(recipes, scenario) -> float | None:
    """Mean kcal as a share of the target (1.0 = on target). Well under 1.0 means thin,
    diet-style dinners."""
    if not recipes or not scenario.max_kcal:
        return None
    return sum(r.per_serving.kcal / scenario.max_kcal for r in recipes) / len(recipes)


def protein_share(recipes) -> float:
    """Mean share of a dinner's energy that comes from protein (~4 kcal/g). A normal dinner
    sits around 0.25-0.30; above ~0.40 it is mostly lean meat."""
    shares = [4 * r.per_serving.protein_g / r.per_serving.kcal for r in recipes if r.per_serving.kcal]
    return sum(shares) / len(shares) if shares else 0.0


# A filling carb base: grains, starchy veg, bread, pulses. Not veg "rice" or green beans.
_CARB_BASE = re.compile(
    r"\b(rice|potato(es)?|pasta|spaghetti|penne|noodles?|gnocchi|bread|pita|tortillas?|wraps?|"
    r"naan|couscous|bulgur|quinoa|barley|oats|farro|polenta|freekeh|beans?|chickpeas?|lentils?)\b")
_NOT_CARB_BASE = re.compile(r"\b(cauliflower|broccoli) rice\b|\bgreen beans?\b")


def carb_base_rate(recipes) -> float:
    """Fraction of dinners with a filling carb base (grain, potato, bread or pulses)."""
    def has_base(r) -> bool:
        return any(_CARB_BASE.search(n) and not _NOT_CARB_BASE.search(n)
                   for n in (i.name.lower() for i in r.ingredients))
    return sum(has_base(r) for r in recipes) / len(recipes) if recipes else 0.0
