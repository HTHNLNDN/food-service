"""Spike (T2): prove USDA FoodData Central access + name -> per-100g macros.

Throwaway discovery script, not production code. Self-contained (reads .env directly).

Design notes it verifies for T4:
- The `/foods/search` response already embeds per-100g nutrients, so ONE call resolves a
  name to macros (no separate /food/{id} detail call — that 404'd for some ids).
- Filtering to Foundation / SR Legacy data types gives clean generic per-100g values.
- The API key goes in the `X-Api-Key` header, never the URL (keeps it out of logs).

Run:   uv run python scripts/spike_usda.py "chicken breast, raw"
Needs: USDA_API_KEY in .env  (free, instant: https://fdc.nal.usda.gov/api-key-signup.html)
"""

import os
import sys

import httpx
from dotenv import load_dotenv

BASE = "https://api.nal.usda.gov/fdc/v1"

# USDA nutrient numbers, reported per 100 g.
NUTRIENT_NUMBERS = {
    "208": "kcal",       # Energy (kcal)
    "203": "protein_g",  # Protein
    "205": "carbs_g",    # Carbohydrate, by difference
    "204": "fat_g",      # Total lipid (fat)
}


def main(query: str) -> None:
    load_dotenv()
    api_key = os.environ.get("USDA_API_KEY")
    if not api_key:
        sys.exit("USDA_API_KEY not set — add it to .env (see the script docstring).")

    headers = {"X-Api-Key": api_key}
    with httpx.Client(timeout=30, headers=headers) as client:
        resp = client.get(
            f"{BASE}/foods/search",
            params={
                "query": query,
                "dataType": ["Foundation", "SR Legacy"],
                "pageSize": 3,
            },
        )
        resp.raise_for_status()
        foods = resp.json().get("foods", [])
        if not foods:
            sys.exit(f"No USDA Foundation/SR-Legacy match for {query!r}")

        top = foods[0]
        print(f"{query!r} -> fdcId={top['fdcId']}  {top.get('description')}  [{top.get('dataType')}]")

        macros = dict.fromkeys(NUTRIENT_NUMBERS.values())
        for n in top.get("foodNutrients", []):
            number = str(n.get("nutrientNumber", ""))
            if number in NUTRIENT_NUMBERS:
                macros[NUTRIENT_NUMBERS[number]] = n.get("value")
        print("per 100 g:", macros)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "chicken breast, raw")
