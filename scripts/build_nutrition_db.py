"""Build a local USDA nutrition store (Foundation + SR Legacy) into data/usda.db.

The FDC CSV zips are NOT committed — drop them in ./usda/ (Foundation + SR Legacy from
https://fdc.nal.usda.gov/download-datasets), then run:

    PYTHONPATH=. uv run python scripts/build_nutrition_db.py

Produces a small, offline, deterministic store of raw/generic foods with per-100 g
kcal / protein / carbs / fat / fibre, plus an FTS5 index over descriptions.
"""

import csv
import io
import sqlite3
import sys
import zipfile
from pathlib import Path

USDA_DIR = Path("usda")
OUT_PATH = Path("data/usda.db")

# Keep only real curated foods — not the raw sample/acquisition records the Foundation zip
# also ships (those have no computed macros and only add noise).
_FOOD_TYPES = {"foundation_food", "sr_legacy_food"}

# USDA nutrient_nbr -> our field. Fibre: prefer 291 (Fiber, total dietary), else 293 (AOAC).
_NUTRIENT_FIELD = {
    "203": "protein_g", "204": "fat_g", "205": "carbs_g", "208": "kcal",
    "291": "fibre_291", "293": "fibre_293",
}

SCHEMA = """
CREATE TABLE foods (
    fdc_id    INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    data_type TEXT NOT NULL,
    kcal      REAL NOT NULL,
    protein_g REAL NOT NULL,
    carbs_g   REAL NOT NULL,
    fat_g     REAL NOT NULL,
    fibre_g   REAL NOT NULL
);
CREATE VIRTUAL TABLE foods_fts USING fts5(description, content='foods', content_rowid='fdc_id',
    tokenize='porter unicode61');  -- porter stemmer: 'potato' matches 'Potatoes, raw'
"""


def _member(zf: zipfile.ZipFile, name: str) -> str:
    for m in zf.namelist():
        if m.endswith("/" + name):
            return m
    raise FileNotFoundError(name)


def _rows(zf: zipfile.ZipFile, name: str):
    with zf.open(_member(zf, name)) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))


def _load_zip(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        nutrient_nbr = {r["id"]: r["nutrient_nbr"] for r in _rows(zf, "nutrient.csv")}
        foods = {
            r["fdc_id"]: {"description": r["description"], "data_type": r["data_type"], "v": {}}
            for r in _rows(zf, "food.csv")
            if r["data_type"] in _FOOD_TYPES
        }
        for r in _rows(zf, "food_nutrient.csv"):
            food = foods.get(r["fdc_id"])
            field = _NUTRIENT_FIELD.get(nutrient_nbr.get(r["nutrient_id"], ""))
            if food is not None and field and r["amount"]:
                food["v"][field] = float(r["amount"])
    return foods


def main() -> None:
    zips = sorted(USDA_DIR.glob("*.zip"))
    if not zips:
        sys.exit(f"No FDC zips in {USDA_DIR}/ — download Foundation + SR Legacy first.")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(OUT_PATH)
    conn.executescript(SCHEMA)

    total = 0
    for zip_path in zips:
        foods = _load_zip(zip_path)
        for fdc_id, food in foods.items():
            v = food["v"]
            protein, carbs, fat = v.get("protein_g", 0.0), v.get("carbs_g", 0.0), v.get("fat_g", 0.0)
            kcal = v.get("kcal")
            if kcal is None:  # Atwater fallback when USDA omits energy (208)
                kcal = 4 * protein + 4 * carbs + 9 * fat
            fibre = v.get("fibre_291", v.get("fibre_293", 0.0))
            conn.execute(
                "INSERT OR REPLACE INTO foods VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(fdc_id), food["description"], food["data_type"], kcal, protein, carbs, fat, fibre),
            )
            total += 1
        print(f"  {zip_path.name}: {len(foods)} foods")

    conn.execute("INSERT INTO foods_fts(rowid, description) SELECT fdc_id, description FROM foods")
    conn.commit()
    conn.close()
    print(f"built {OUT_PATH} with {total} foods")


if __name__ == "__main__":
    main()
