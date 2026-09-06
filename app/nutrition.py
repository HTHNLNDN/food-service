"""Deterministic macro engine over a LOCAL USDA store (built by scripts/build_nutrition_db.py).

The numbers always come from the store (offline, reproducible). Resolving a free-text
ingredient to the right food is a semantic problem, so we retrieve candidates by full-text
search and let an LLM pick the best one — the LLM chooses *which real food*, never the
numbers — and the choice is cached, so lookups are deterministic after the first.
"""

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

import httpx

from app import telemetry
from app.llm import chat_completion


@dataclass(frozen=True)
class Ingredient:
    name: str
    grams: float


@dataclass(frozen=True)
class Macros:
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fibre_g: float = 0.0


@dataclass(frozen=True)
class MacroResult:
    total: Macros
    unresolved: list[str]


class NutritionSource(Protocol):
    def lookup(self, name: str) -> tuple[int, Macros] | None:
        """Return (fdc_id, per-100g macros), or None if the name can't be resolved."""
        ...


def macros_for(ingredients: list[Ingredient], source: NutritionSource) -> MacroResult:
    """Sum per-ingredient macros from the food store. Pure + deterministic — no LLM numbers.

    Unresolved ingredients are surfaced by name and excluded from the total, never counted
    as zero silently (which would understate a recipe's real macros).
    """
    kcal = protein = carbs = fat = fibre = 0.0
    unresolved: list[str] = []
    for ing in ingredients:
        found = source.lookup(ing.name)
        if found is None:
            unresolved.append(ing.name)
            continue
        _fdc_id, per100 = found
        factor = ing.grams / 100.0
        kcal += per100.kcal * factor
        protein += per100.protein_g * factor
        carbs += per100.carbs_g * factor
        fat += per100.fat_g * factor
        fibre += per100.fibre_g * factor
    return MacroResult(Macros(kcal, protein, carbs, fat, fibre), unresolved)


# --- candidate selection ---

_MISS = object()


@dataclass(frozen=True)
class Candidate:
    fdc_id: int
    description: str


class CandidatePicker(Protocol):
    def pick(self, ingredient: str, candidates: list[Candidate]) -> int | None:
        """Choose the fdc_id of the best-matching candidate, or None if none fit."""
        ...


def _fts_query(name: str) -> str:
    # OR the alphanumeric tokens so relevant foods are recalled; bm25 rank surfaces the best.
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return " OR ".join(f'"{tok}"' for tok in tokens)


# Curated staples: hand-verified fdc_ids for common dinner ingredients. Pinning these
# guarantees the right food AND skips the LLM picker (faster/cheaper). Misses fall through
# to FTS + picker; an id absent from the store (dataset drift) also falls through.
STAPLE_ALIASES: dict[str, int] = {
    # proteins
    "chicken breast": 2727569, "chicken thigh": 2727567, "ground beef": 168608,
    "beef steak": 2727573, "pork chop": 2727575, "salmon": 173689, "cod": 171955,
    "tuna": 173706, "shrimp": 175179, "egg": 171287, "eggs": 171287, "tofu": 172475,
    "turkey breast": 171098,
    # dairy
    "greek yogurt": 330137, "milk": 171265, "cheddar cheese": 328637, "cheddar": 328637,
    "feta": 173420, "mozzarella": 169051, "parmesan": 170848, "butter": 173410,
    # grains / carbs
    "white rice": 168879, "rice": 168879, "brown rice": 2512380, "pasta": 169736,
    "potato": 170026, "potatoes": 170026, "sweet potato": 168482, "oats": 2346396,
    "quinoa": 168874,
    # vegetables
    "broccoli": 170379, "cauliflower": 169986, "carrot": 170393, "carrots": 170393,
    "onion": 170000, "onions": 170000, "garlic": 169230, "spinach": 168462,
    "tomato": 170457, "tomatoes": 170457, "cherry tomatoes": 170457, "cherry tomato": 170457,
    "grape tomatoes": 170457, "baby tomatoes": 170457, "bell pepper": 2258590, "zucchini": 169291,
    "courgette": 169291, "mushroom": 169251, "mushrooms": 169251, "cucumber": 168409,
    "peas": 170419, "green beans": 169961, "kale": 168421, "cabbage": 169975,
    # legumes
    "chickpeas": 173756, "lentils": 172420, "black beans": 173734,
    # fats / other
    "olive oil": 171413, "avocado": 171707, "lemon": 167746,
}


class LocalNutritionSource:
    """Resolve ingredient -> (fdc_id, macros) using the local store + a candidate picker."""

    def __init__(
        self,
        store: sqlite3.Connection,
        cache: sqlite3.Connection,
        picker: CandidatePicker,
        candidate_limit: int = 20,
        aliases: dict[str, int] | None = None,
    ):
        self.store = store
        self.cache = cache
        self.picker = picker
        self.candidate_limit = candidate_limit
        self.aliases = STAPLE_ALIASES if aliases is None else aliases

    def lookup(self, name: str) -> tuple[int, Macros] | None:
        key = name.strip().lower()

        alias = self.aliases.get(key)  # curated staples: exact, verified, no LLM call
        if alias is not None:
            found = self._macros(alias)
            if found is not None:
                return found

        cached = self._cached_fdc_id(key)
        if cached is not _MISS:
            return self._macros(cached) if cached is not None else None

        candidates = self._search(key)
        if not candidates:
            self._remember(key, None)
            return None
        chosen = self.picker.pick(name, candidates)
        valid = chosen if any(c.fdc_id == chosen for c in candidates) else None
        self._remember(key, valid)
        return self._macros(valid) if valid is not None else None

    def warm(self, names: list[str]) -> None:
        """Pre-resolve uncached ingredients so a plan's macro checks don't block on sequential
        LLM picks. FTS and cache I/O stay on this thread (sqlite is single-thread); only the
        independent, network-bound picker calls run concurrently."""
        todo: list[tuple[str, str, list[Candidate]]] = []
        seen: set[str] = set()
        for name in names:
            key = name.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            if self.aliases.get(key) is not None and self._macros(self.aliases[key]) is not None:
                continue  # curated alias, no pick needed
            if self._cached_fdc_id(key) is not _MISS:
                continue  # already resolved (or a cached miss)
            candidates = self._search(key)
            if not candidates:
                self._remember(key, None)
                continue
            todo.append((name, key, candidates))
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            picks = list(pool.map(lambda t: self.picker.pick(t[0], t[2]), todo))
        for (_name, key, candidates), chosen in zip(todo, picks):
            valid = chosen if any(c.fdc_id == chosen for c in candidates) else None
            self._remember(key, valid)

    def _search(self, name: str) -> list[Candidate]:
        query = _fts_query(name)
        if not query:
            return []
        rows = self.store.execute(
            "SELECT f.fdc_id, f.description FROM foods_fts x JOIN foods f ON f.fdc_id = x.rowid "
            "WHERE foods_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, self.candidate_limit),
        ).fetchall()
        return [Candidate(fdc_id=r[0], description=r[1]) for r in rows]

    def _macros(self, fdc_id: int) -> tuple[int, Macros] | None:
        row = self.store.execute(
            "SELECT kcal, protein_g, carbs_g, fat_g, fibre_g FROM foods WHERE fdc_id = ?",
            (fdc_id,),
        ).fetchone()
        if row is None:
            return None
        return (fdc_id, Macros(row[0], row[1], row[2], row[3], row[4]))

    def _cached_fdc_id(self, key: str):
        row = self.cache.execute("SELECT fdc_id FROM resolve_cache WHERE name = ?", (key,)).fetchone()
        return _MISS if row is None else row["fdc_id"]

    def _remember(self, key: str, fdc_id: int | None) -> None:
        self.cache.execute("INSERT OR REPLACE INTO resolve_cache(name, fdc_id) VALUES (?, ?)", (key, fdc_id))
        self.cache.commit()


class LLMCandidatePicker:
    """Picks the best USDA food for an ingredient via any OpenAI-compatible chat model.

    The model only chooses *which real food* matches — it never produces macro numbers.
    """

    _SYSTEM = (
        "You match a recipe ingredient to the single best USDA food from a numbered list. "
        "Prefer the raw/plain/generic form as used in home cooking — e.g. the raw fish fillet, "
        "not fish oil or a breaded product; whole egg, not egg white or egg bread; the raw "
        "vegetable, not a casserole or chips. Reply with ONLY the numeric fdc_id of the best "
        "food, or the word 'none' if nothing reasonably matches."
    )

    def __init__(self, base_url: str, model: str, api_key: str, client: httpx.Client | None = None):
        self.model = model
        self._client = client or httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=60
        )

    def pick(self, ingredient: str, candidates: list[Candidate]) -> int | None:
        listing = "\n".join(f"{c.fdc_id}: {c.description}" for c in candidates)
        messages = [
            {"role": "system", "content": self._SYSTEM},
            {"role": "user", "content": f"Ingredient: {ingredient}\n\nFoods:\n{listing}\n\nBest fdc_id:"},
        ]
        with telemetry.record("nutrition_pick", "openai-compat", self.model):
            content = chat_completion(self._client, self.model, messages)
        match = re.search(r"\d+", content)
        return int(match.group()) if match else None
