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
from pathlib import Path
from typing import Protocol

import httpx
import psycopg

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


class _LazyUsdaStore:
    """The local USDA store (scripts/build_nutrition_db.py), opened read-only on first query.

    Lazy because services are built for every request that needs any of them (shopping, UI
    translation) — only an actual food lookup should need the file. A missing file then fails
    at that lookup instead of silently creating an empty store."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None

    def execute(self, sql: str, params=()):
        if self._conn is None:
            self._conn = sqlite3.connect(f"{self._path.resolve().as_uri()}?mode=ro", uri=True)
        return self._conn.execute(sql, params)


def open_usda_store(path: Path) -> _LazyUsdaStore:
    return _LazyUsdaStore(path)


class LocalNutritionSource:
    """Resolve ingredient -> (fdc_id, macros) using the local store + a candidate picker.

    Curated staples come first: the store's `aliases` table (from scripts/staple_aliases.csv)
    pins common ingredients to hand-verified foods, so they skip the LLM picker. Misses, and an
    alias whose food is missing, fall through to FTS + picker."""

    def __init__(
        self,
        store: sqlite3.Connection | _LazyUsdaStore,  # read-only USDA store (open_usda_store)
        cache: psycopg.Connection,  # app DB: resolve_cache
        picker: CandidatePicker,
        candidate_limit: int = 20,
    ):
        self.store = store
        self.cache = cache
        self.picker = picker
        self.candidate_limit = candidate_limit

    def lookup(self, name: str) -> tuple[int, Macros] | None:
        key = name.strip().lower()

        alias = self._alias(key)  # curated staples: exact, verified, no LLM call
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
            alias = self._alias(key)
            if alias is not None and self._macros(alias) is not None:
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

    def _alias(self, key: str) -> int | None:
        try:
            row = self.store.execute("SELECT fdc_id FROM aliases WHERE name = ?", (key,)).fetchone()
        except sqlite3.OperationalError:  # a store built before the aliases table: no aliases
            return None
        return row[0] if row else None

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
        row = self.cache.execute("SELECT fdc_id FROM resolve_cache WHERE name = %s", (key,)).fetchone()
        return _MISS if row is None else row["fdc_id"]

    def _remember(self, key: str, fdc_id: int | None) -> None:
        self.cache.execute(
            "INSERT INTO resolve_cache(name, fdc_id) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET fdc_id = excluded.fdc_id", (key, fdc_id))


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
