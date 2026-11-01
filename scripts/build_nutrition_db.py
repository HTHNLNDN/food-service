"""Build a local USDA nutrition store (Foundation + SR Legacy) into data/usda.db.

The FDC CSV zips are NOT committed — drop them in ./usda/ (Foundation + SR Legacy from
https://fdc.nal.usda.gov/download-datasets), then run:

    PYTHONPATH=. uv run python scripts/build_nutrition_db.py
    PYTHONPATH=. uv run python scripts/build_nutrition_db.py --aliases-only  # reload aliases

Produces a small, offline, deterministic store of raw/generic foods with per-100 g
kcal / protein / carbs / fat / fibre, an FTS5 index over descriptions, and an `aliases` table:
common ingredient names pinned to hand-verified foods (from staple_aliases.csv next to this
script), so the app resolves them without asking an LLM. `--aliases-only` reloads just that
table into an existing store, e.g. after editing the CSV; copy the store to the server after.
"""

import argparse
import csv
import io
import sqlite3
import sys
import zipfile
from pathlib import Path

USDA_DIR = Path("usda")
OUT_PATH = Path("data/usda.db")
ALIASES_CSV = Path(__file__).resolve().parent / "staple_aliases.csv"

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


def load_aliases(conn: sqlite3.Connection, csv_path: Path = ALIASES_CSV) -> int:
    """(Re)write the `aliases` table from the curated CSV (name,fdc_id). Refuses, writing
    nothing, if any alias points at a food missing from this store or with zero kcal: some
    USDA records carry no macros at all, and an alias to one would silently count as 0."""
    with csv_path.open(newline="") as fh:
        rows = [(r["name"].strip().lower(), int(r["fdc_id"])) for r in csv.DictReader(fh)]
    bad = []
    for name, fdc_id in rows:
        food = conn.execute("SELECT kcal FROM foods WHERE fdc_id = ?", (fdc_id,)).fetchone()
        if food is None or food[0] <= 0:
            bad.append(f"{name} -> {fdc_id}")
    if bad:
        raise ValueError("aliases point at missing or zero-macro foods: " + ", ".join(bad))
    with conn:
        conn.execute("DROP TABLE IF EXISTS aliases")
        conn.execute("CREATE TABLE aliases (name TEXT PRIMARY KEY, fdc_id INTEGER NOT NULL)")
        conn.executemany("INSERT INTO aliases VALUES (?, ?)", rows)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the local USDA nutrition store.")
    ap.add_argument("--aliases-only", action="store_true",
                    help=f"only reload the aliases table into an existing {OUT_PATH}")
    args = ap.parse_args()
    if args.aliases_only:
        if not OUT_PATH.exists():
            sys.exit(f"No store at {OUT_PATH}: build it first (without --aliases-only).")
        conn = sqlite3.connect(OUT_PATH)
        print(f"loaded {load_aliases(conn)} aliases into {OUT_PATH}")
        conn.close()
        return

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
    aliases = load_aliases(conn)
    conn.close()
    print(f"built {OUT_PATH} with {total} foods and {aliases} aliases")


if __name__ == "__main__":
    main()
