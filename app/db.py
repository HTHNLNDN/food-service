import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

# Schema grows one slice at a time. Single source of truth for all tables.
SCHEMA = """
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- profile slice --
CREATE TABLE IF NOT EXISTS preferences (
    id            INTEGER PRIMARY KEY CHECK (id = 1),  -- single household row
    max_kcal      INTEGER,
    min_protein_g INTEGER,
    restrictions  TEXT    NOT NULL DEFAULT '',
    servings      INTEGER NOT NULL DEFAULT 2,
    bans          TEXT    NOT NULL DEFAULT '[]'  -- JSON list of banned category ids + custom items
);

CREATE TABLE IF NOT EXISTS stores (
    chain_slug TEXT PRIMARY KEY  -- the user's store whitelist
);

CREATE TABLE IF NOT EXISTS ratings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    score      INTEGER NOT NULL,  -- >0 liked, <0 disliked
    tags       TEXT NOT NULL,     -- JSON list of ingredient tags
    created_at TEXT NOT NULL
);

-- nutrition slice (USDA caches) --
CREATE TABLE IF NOT EXISTS resolve_cache (
    name   TEXT PRIMARY KEY,  -- normalized ingredient name
    fdc_id INTEGER            -- NULL = confirmed no match (negative cache)
);

CREATE TABLE IF NOT EXISTS nutrition_cache (
    fdc_id    INTEGER PRIMARY KEY,
    kcal      REAL NOT NULL,
    protein_g REAL NOT NULL,
    carbs_g   REAL NOT NULL,
    fat_g     REAL NOT NULL
);

-- LLM cost/telemetry: one append-only row per model call.
-- Attribute names mirror the OpenTelemetry GenAI semantic conventions for later portability.
CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    tenant_id      TEXT NOT NULL DEFAULT 'me',
    trace_id       TEXT,               -- groups all calls of one operation (e.g. a plan)
    plan_id        INTEGER,            -- linked after the plan persists
    call_type      TEXT NOT NULL,      -- plan_week | replace | nutrition_pick | offer_match
    provider       TEXT NOT NULL,      -- gemini | openai-compat | ...
    model          TEXT NOT NULL,
    grounded       INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    thinking_tokens INTEGER,
    total_tokens   INTEGER,
    search_queries INTEGER NOT NULL DEFAULT 0,  -- billable Google Search grounding queries
    retries        INTEGER NOT NULL DEFAULT 0,  -- transient retries + JSON re-prompts
    latency_ms     INTEGER,
    ok             INTEGER NOT NULL,
    finish_reason  TEXT,
    error          TEXT
);

-- Offline eval results (evals/run.py): one run row + many (scenario, metric, value) scores,
-- so prompt/model changes can be compared run-over-run.
CREATE TABLE IF NOT EXISTS eval_runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    model   TEXT,
    samples INTEGER,
    notes   TEXT
);
CREATE TABLE IF NOT EXISTS eval_scores (
    run_id   INTEGER NOT NULL REFERENCES eval_runs(id),
    scenario TEXT NOT NULL,
    metric   TEXT NOT NULL,
    value    REAL
);

-- Static UI-chrome strings (nav, headings, buttons — see app/i18n.py), translated once per
-- configured display language and cached so page renders never re-call the LLM.
CREATE TABLE IF NOT EXISTS ui_translations (
    language TEXT NOT NULL,
    text     TEXT NOT NULL,  -- the literal English string as written in the templates
    value    TEXT NOT NULL,
    PRIMARY KEY (language, text)
);

-- Editable price list; cost is computed as a view over this so a price change re-costs history.
CREATE TABLE IF NOT EXISTS model_prices (
    model           TEXT PRIMARY KEY,
    input_per_mtok  REAL NOT NULL DEFAULT 0,  -- $ per 1M input tokens
    output_per_mtok REAL NOT NULL DEFAULT 0,  -- $ per 1M output tokens
    search_per_ktok REAL NOT NULL DEFAULT 0,  -- $ per 1k grounding queries
    updated         TEXT
);

-- Ingredients the local food store couldn't resolve, rolled up for periodic review: add the
-- frequent/revise-causing ones to the store (with real macros looked up) in batches.
CREATE TABLE IF NOT EXISTS missing_ingredients (
    name          TEXT PRIMARY KEY,             -- normalized ingredient name (as the model wrote it)
    hits          INTEGER NOT NULL DEFAULT 0,   -- times seen unresolved while planning
    caused_revise INTEGER NOT NULL DEFAULT 0,   -- times it was big enough to force a dish revise
    sample_grams  REAL,                         -- a representative amount seen
    last_seen     TEXT NOT NULL
);

-- offers slice --
CREATE TABLE IF NOT EXISTS offers_cache (
    chain_slug TEXT NOT NULL,
    week       TEXT NOT NULL,   -- ISO year-week, e.g. 2026-W36
    payload    TEXT NOT NULL,   -- JSON list of raw offers as returned by ShelfAtlas
    PRIMARY KEY (chain_slug, week)
);

-- shopping slice: cached ingredient->offer picks so page renders don't re-call the LLM.
-- Keyed by week because offers reset weekly; NULL offer_name = confirmed no match (negative).
CREATE TABLE IF NOT EXISTS offer_match_cache (
    week       TEXT NOT NULL,
    name       TEXT NOT NULL,   -- normalized ingredient name
    offer_name TEXT,            -- matched offer's raw name; NULL when nothing fit
    price      TEXT,            -- Decimal as string; NULL when unmatched
    chain_slug TEXT,            -- NULL when unmatched
    PRIMARY KEY (week, name)
);

-- planner slice --
CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    week       TEXT NOT NULL,
    use_up     TEXT NOT NULL DEFAULT ''  -- free-text ingredients the user wants to use up
);

CREATE TABLE IF NOT EXISTS recipes (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id               INTEGER NOT NULL REFERENCES plans(id),
    slot_index            INTEGER NOT NULL,
    title                 TEXT NOT NULL,
    servings              INTEGER NOT NULL,
    steps                 TEXT NOT NULL,  -- JSON list of strings
    per_serving_kcal      REAL,
    per_serving_protein_g REAL,
    per_serving_carbs_g   REAL,
    per_serving_fat_g     REAL,
    per_serving_fibre_g   REAL,
    flagged               INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    name      TEXT NOT NULL,
    grams     REAL NOT NULL
);

-- Per-call cost = tokens x price. LEFT JOIN so an unpriced model shows 0 (a signal to add a price).
CREATE VIEW IF NOT EXISTS v_llm_cost AS
SELECT c.*,
    COALESCE(c.input_tokens, 0)  / 1e6 * COALESCE(p.input_per_mtok, 0)
  + COALESCE(c.output_tokens, 0) / 1e6 * COALESCE(p.output_per_mtok, 0)
  + COALESCE(c.search_queries, 0)/ 1e3 * COALESCE(p.search_per_ktok, 0) AS usd
FROM llm_calls c LEFT JOIN model_prices p ON p.model = c.model;
"""

# Seeded once (INSERT OR IGNORE — never overwrites your edits). ESTIMATES — verify against the
# provider's current pricing; edit the model_prices table to correct them.
DEFAULT_PRICES = [
    ("gemini-3.6-flash", 0.30, 2.50, 14.0),
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating the parent dir if needed) a SQLite connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")  # wait, don't error, if the bg planner holds a write lock
    return conn


# Columns added to existing tables after their first release. SQLite has no
# "ADD COLUMN IF NOT EXISTS", so each ALTER is applied guarded (idempotent).
_MIGRATIONS = [
    ("preferences", "bans", "TEXT NOT NULL DEFAULT '[]'"),
    ("recipes", "per_serving_fibre_g", "REAL"),
    ("plans", "use_up", "TEXT NOT NULL DEFAULT ''"),
    # Display-only translation (app/translate.py): empty language = English, unchanged behaviour.
    ("preferences", "language", "TEXT NOT NULL DEFAULT ''"),
    ("recipes", "title_translated", "TEXT"),
    ("recipes", "steps_translated", "TEXT"),  # JSON list, same length/order as `steps`
    ("recipe_ingredients", "name_translated", "TEXT"),
]


def bootstrap(conn: sqlite3.Connection) -> None:
    """Apply the schema and idempotent column migrations. Safe to run on every startup."""
    conn.executescript(SCHEMA)
    for table, column, coldef in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.executemany(
        "INSERT OR IGNORE INTO model_prices (model, input_per_mtok, output_per_mtok, search_per_ktok) "
        "VALUES (?, ?, ?, ?)",
        DEFAULT_PRICES,
    )
    conn.commit()


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: a per-request connection to the app's SQLite file."""
    conn = connect(request.app.state.config.db_path)
    try:
        yield conn
    finally:
        conn.close()


# Shared FastAPI dependency annotation for route handlers.
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
