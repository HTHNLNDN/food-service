# food-service

A small, single-tenant agentic app that builds a weekday (Mon–Fri) dinner plan for
n-amount of people, grounded in current Danish grocery offers, with **deterministic** macros and a
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
| `auth` | `app/auth.py`, `app/users.py` | accounts (self sign-up), login sessions, per-user data; one-time data-claim CLI |

App state lives in **PostgreSQL**: `app/db.py` provides the connection pool, and the schema lives in
Alembic migrations under `migrations/`.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env      # then fill in the keys (see below)
```

### Database (local Postgres)

The app needs a PostgreSQL 18 database at `DATABASE_URL`. For local development:

```bash
docker run -d --name food-service-dev-db -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=food -e POSTGRES_PASSWORD=food -e POSTGRES_DB=food_service \
  -v food-service-dev-pgdata:/var/lib/postgresql postgres:18
# .env: DATABASE_URL=postgresql://food:food@127.0.0.1:5432/food_service
uv run alembic upgrade head   # create/upgrade the schema (run after every pull)
```

New schema change? Add a migration under `migrations/versions/` (hand-written SQL via
`op.execute`). It must keep the previous app version working: expand now, contract in a
later release (see DEPLOY.md).

### Nutrition store (local, offline)

Nutrition macros come from a **local USDA store**, not an API. Download the **Foundation**
and **SR Legacy** CSV zips from https://fdc.nal.usda.gov/download-datasets into `./usda/`, then:

```bash
PYTHONPATH=. uv run python scripts/build_nutrition_db.py   # builds data/usda.db (~8k foods)
# the app reads it from USDA_DB_PATH (default data/usda.db), read-only
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
# open http://127.0.0.1:8000/signup and create an account with your SIGNUP_CODE
```

Each account has its own preferences, stores, ratings, plans and shopping list. People create
their own account with the invite code (`SIGNUP_CODE`). Sign-in lasts 90 days per device while
it's used ("Keep me signed in"), and the forms are set up so browsers and phones offer to save
the password. Usernames are stored only as a keyed hash (`USERNAME_KEY`) and passwords only as
Argon2id hashes. There's **no password reset**, by design.

## Develop

```bash
uv run pytest          # tests (deterministic slices tested hard; agent/offers mocked)
                       # DB tests start a throwaway postgres:18-alpine: needs Docker running
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
  recipe level. This also means that if a name brand dtable in onoffer, that will be shown, which is likely more expensive than the dkscount counterpart.
- **KISS:** plain Postgres tables (not a vector DB), plain form POSTs (not HTMX), one adapter for all
  providers.

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — free to clone, run, and modify for personal,
noncommercial use. Commercial use (inside a company, or as part of something you sell)
requires a separate agreement with the author.
