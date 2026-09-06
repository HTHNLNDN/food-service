# food-service

A small, single-tenant agentic app that builds a weekday (Mon–Fri) lunch + dinner plan for
two people, grounded in current Danish grocery offers, with **deterministic** macros and a
**provider-agnostic** recipe agent. Local-first — runs on your machine, no cloud, no accounts.

> Built for me and my wife; shared under a noncommercial license so anyone can clone and
> tinker with it for personal use. Not a hosted service. See [License](#license).

## What it does

1. You set preferences (calorie/protein targets, restrictions like PCOS/vegan, the DK stores
   you shop at).
2. "Plan this week" pulls current offers for your stores (ShelfAtlas) and an LLM writes
   weekday dinners biased toward what's on sale.
3. **Every macro is recomputed deterministically** from USDA FoodData Central — the LLM is
   never trusted for numbers. Dishes that miss your targets are revised, then flagged.
4. Accept/decline dishes, rate them (👍/👎) to shape future weeks, print the recipes, and
   export all your data.

## Architecture (vertical slices)

| Slice | File | Responsibility |
|---|---|---|
| `profile` | `app/profile.py` | preferences, store picker, ratings → preference summary, data export |
| `nutrition` | `app/nutrition.py` | **deterministic** macro engine over USDA (the determinism wall) |
| `offers` | `app/offers.py` | ShelfAtlas offers, strict store whitelist, weekly cache |
| `agent` | `app/agent.py` | provider-neutral `MealPlanAgent` (OpenAI-compatible) |
| `planner` | `app/planner.py` | assemble context → generate → verify/revise → persist |
| `shopping` | `app/shopping.py` | cheapest-match shopping list among selected stores |
| `ui` | `app/ui.py` + `templates/` | soft/light web UI, print view |

`app/db.py` holds the single SQLite schema.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env      # then fill in the keys (see below)
```

### Nutrition store (local, offline)

Nutrition macros come from a **local USDA store**, not an API. Download the **Foundation**
and **SR Legacy** CSV zips from https://fdc.nal.usda.gov/download-datasets into `./usda/`, then:

```bash
PYTHONPATH=. uv run python scripts/build_nutrition_db.py   # builds data/usda.db (~8k foods)
```

Ingredient→food resolution uses FTS5 + an LLM that picks *which* food (the macro numbers
always come from the store, never the model), so it's deterministic and reproducible.

### API keys (all free tiers)

Put these in `.env` (gitignored):

- **`SHELFATLAS_API_KEY`** — https://app.shelfatlas.com (free tier, `sa_live_…`)
- **`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`** — any OpenAI-compatible provider (used for
  both recipe generation *and* nutrition food-picking). Presets (Anthropic, Gemini, DeepSeek,
  OpenRouter, local Ollama) are in `.env.example`. Swap providers by changing these three values.

## Run

```bash
uv run uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

## Develop

```bash
uv run pytest          # tests (deterministic slices tested hard; agent/offers mocked)
uv run ruff check .    # lint
uv run ruff format .   # format
```

**End-to-end UI test** (Playwright): drives the real app in a headless browser with fake
services — the pattern to extend when adding UI features. One-time browser install:

```bash
uv run playwright install chromium
uv run pytest tests/test_e2e_ui.py
```

## Design notes

- **The LLM is only the creative layer.** It outputs `{ingredient, grams}`; the `nutrition`
  slice computes macros. This is why macros are trustworthy regardless of model.
- **Offers are Danish free-text** (no product IDs), so ingredient→offer matching is
  best-effort; unmatched items are listed at unknown price. Offer *biasing* happens at the
  recipe level.
- **KISS:** SQLite (not a vector DB), plain form POSTs (not HTMX), one adapter for all
  providers.

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — free to clone, run, and modify for personal,
noncommercial use. Commercial use (inside a company, or as part of something you sell)
requires a separate agreement with the author.
