import sqlite3

import httpx
import pytest

from app.db import bootstrap, connect
from app.nutrition import (
    Candidate,
    Ingredient,
    LLMCandidatePicker,
    LocalNutritionSource,
    Macros,
    macros_for,
)


class FakeSource:
    def __init__(self, table: dict[str, tuple[int, Macros]]):
        self.table = table

    def lookup(self, name: str):
        return self.table.get(name)


# per-100g reference values (kcal, protein, carbs, fat, fibre)
CHICKEN = Macros(120, 23, 0, 2.6, 0)
RICE = Macros(130, 2.7, 28, 0.3, 0.4)
BROCCOLI = Macros(34, 2.8, 7, 0.4, 2.6)

SOURCE = FakeSource({"chicken breast": (1, CHICKEN), "white rice": (2, RICE), "broccoli": (3, BROCCOLI)})
RECIPE = [Ingredient("chicken breast", 200), Ingredient("white rice", 150), Ingredient("broccoli", 100)]


def test_macros_for_sums_scaled_including_fibre():
    result = macros_for(RECIPE, SOURCE)
    assert result.total.kcal == pytest.approx(469.0)
    assert result.total.protein_g == pytest.approx(52.85)
    assert result.total.fibre_g == pytest.approx(0.4 * 1.5 + 2.6)  # rice + broccoli
    assert result.unresolved == []


def test_macros_for_surfaces_unresolved():
    result = macros_for([*RECIPE, Ingredient("unicorn meat", 100)], SOURCE)
    assert result.unresolved == ["unicorn meat"]
    assert result.total.protein_g == pytest.approx(52.85)


def test_macros_for_is_deterministic():
    assert macros_for(RECIPE, SOURCE) == macros_for(RECIPE, SOURCE)


# --- LocalNutritionSource (seeded in-memory store + fake picker) ---


class FakePicker:
    def __init__(self, choice):
        self.choice = choice
        self.calls = 0

    def pick(self, ingredient, candidates):
        self.calls += 1
        return self.choice


def _store():
    s = sqlite3.connect(":memory:")
    s.executescript(
        "CREATE TABLE foods (fdc_id INTEGER PRIMARY KEY, description TEXT, data_type TEXT, "
        "kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL, fibre_g REAL);"
        "CREATE VIRTUAL TABLE foods_fts USING fts5(description, content='foods', content_rowid='fdc_id', tokenize='porter unicode61');"
    )
    s.executemany("INSERT INTO foods VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (1, "Fish oil, salmon", "sr_legacy_food", 902, 0, 0, 100, 0),
        (2, "Fish, salmon, raw", "sr_legacy_food", 180, 20, 0, 11, 0),
        (3, "Broccoli, raw", "foundation_food", 34, 2.8, 7, 0.4, 2.6),
    ])
    s.execute("INSERT INTO foods_fts(rowid, description) SELECT fdc_id, description FROM foods")
    s.commit()
    return s


def _cache(tmp_path):
    c = connect(tmp_path / "c.db")
    bootstrap(c)
    return c


def test_local_lookup_returns_macros_of_picked_food(tmp_path):
    src = LocalNutritionSource(_store(), _cache(tmp_path), FakePicker(2))
    found = src.lookup("salmon")
    assert found is not None and found[0] == 2
    assert found[1].protein_g == 20 and found[1].kcal == 180  # raw flesh, not the oil (fdc 1)


def test_local_lookup_carries_fibre(tmp_path):
    src = LocalNutritionSource(_store(), _cache(tmp_path), FakePicker(3))
    _, macros = src.lookup("broccoli")
    assert macros.fibre_g == 2.6


def test_local_lookup_none_when_no_candidates(tmp_path):
    src = LocalNutritionSource(_store(), _cache(tmp_path), FakePicker(2))
    assert src.lookup("zzznotafood") is None


def test_local_lookup_none_when_picker_declines(tmp_path):
    src = LocalNutritionSource(_store(), _cache(tmp_path), FakePicker(None))
    assert src.lookup("salmon") is None


def test_local_lookup_rejects_fdc_id_not_among_candidates(tmp_path):
    src = LocalNutritionSource(_store(), _cache(tmp_path), FakePicker(999))
    assert src.lookup("salmon") is None


def test_local_lookup_caches_and_skips_picker_on_repeat(tmp_path):
    picker = FakePicker(2)
    src = LocalNutritionSource(_store(), _cache(tmp_path), picker, aliases={})
    src.lookup("salmon")
    src.lookup("salmon")
    assert picker.calls == 1


def test_curated_alias_pins_food_and_skips_picker(tmp_path):
    picker = FakePicker(1)  # would wrongly pick the oil
    src = LocalNutritionSource(_store(), _cache(tmp_path), picker, aliases={"salmon": 2})
    found = src.lookup("salmon")
    assert found is not None and found[0] == 2  # the pinned raw flesh
    assert picker.calls == 0  # alias short-circuits the LLM


def test_warm_prepopulates_cache_so_lookups_skip_the_picker(tmp_path):
    picker = FakePicker(2)
    src = LocalNutritionSource(_store(), _cache(tmp_path), picker, aliases={})
    src.warm(["salmon", "broccoli", "salmon"])  # dedupes -> one pick per unique uncached name
    assert picker.calls == 2
    src.lookup("salmon")
    src.lookup("broccoli")
    assert picker.calls == 2  # served from the warmed cache, no further picks


def test_alias_with_unknown_fdc_falls_through_to_picker(tmp_path):
    picker = FakePicker(3)
    src = LocalNutritionSource(_store(), _cache(tmp_path), picker, aliases={"broccoli": 99999})
    found = src.lookup("broccoli")
    assert found is not None and found[0] == 3  # 99999 not in store -> picker used
    assert picker.calls == 1


# --- LLMCandidatePicker (mocked httpx) ---


def _picker(content):
    client = httpx.Client(
        base_url="https://llm.test/v1",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"choices": [{"message": {"content": content}}]})),
    )
    return LLMCandidatePicker("https://llm.test/v1", "m", "k", client=client)


CANDS = [Candidate(1, "Fish oil, salmon"), Candidate(2, "Fish, salmon, raw")]


def test_llm_picker_returns_chosen_fdc_id():
    assert _picker("2").pick("salmon", CANDS) == 2


def test_llm_picker_returns_none_when_model_declines():
    assert _picker("none").pick("salmon", CANDS) is None
