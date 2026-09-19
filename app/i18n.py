"""Static UI-chrome strings: nav links, headings, button labels — the text that lives directly
in templates rather than being generated per-recipe (that's app/translate.py). Templates call
`t("English literal")`; UI_STRINGS below must list every literal passed to `t(...)` so it can be
batch-translated in one call and cached in `ui_translations`. Any string not yet cached, or a
translator that fails, just falls back to English — this must never block a page render.
"""

import sqlite3

UI_STRINGS: list[str] = [
    # nav / page titles (base.html) — shared with each page's `title` context value
    "This week", "Shopping", "Preferences", "Cost", "My data", "Recipes", "Week", "More",
    # plan.html
    "This week's dinners", "Planning your week…",
    "No plan yet. Pick your preferences, then plan the week.",
    "Using up any ingredients this week?", "(optional)", "Shopping list",
    "Print recipes", "Re-plan this week", "Plan this week",
    "Writing recipes and checking macros against your targets.",
    ("This usually takes a minute or two. The page updates on its own — you can lock your phone "
     "and come back."),
    "Writing recipes and checking macros against your targets — this can take a minute or two.",
    "Macros could not be verified within targets", "off-target", "Ingredients & steps",
    "Like — more like this", "Dislike — avoid this", "Decline & replace",
    "Finding a replacement…", "Weekday schedule", "Day", "Dinner", "View shopping list",
    # shopping.html
    "No plan yet.", "Plan your week", "first — your shopping list will appear here.",
    "Choose the stores you shop at in", "to build a shopping list.", "On offer",
    "Not on offer", "Nothing to buy — everything's covered.", "Already have",
    "— using up, don't buy",
    # print.html
    "No plan to print yet.", "Print these recipes", "Ingredients", "Steps", "Save",
]


def cached(conn: sqlite3.Connection, language: str) -> dict[str, str]:
    """text -> translation already cached for `language` ({} for English / no language set)."""
    if not language:
        return {}
    return {
        row["text"]: row["value"]
        for row in conn.execute(
            "SELECT text, value FROM ui_translations WHERE language = ?", (language,)
        )
    }


def warm(conn: sqlite3.Connection, language: str, translator) -> None:
    """Batch-translate any UI_STRINGS not yet cached for `language`. Best-effort: no language, no
    translator, a malformed reply, or any exception all leave the cache untouched — callers keep
    seeing English for whatever isn't cached, nothing breaks."""
    if not language or translator is None:
        return
    missing = [s for s in UI_STRINGS if s not in cached(conn, language)]
    if not missing:
        return
    try:
        translated = translator.translate_batch(missing, language)
    except Exception:  # noqa: BLE001 - a translation hiccup must not break the profile save
        return
    if translated is None or len(translated) != len(missing):
        return
    conn.executemany(
        "INSERT OR REPLACE INTO ui_translations (language, text, value) VALUES (?, ?, ?)",
        list(zip([language] * len(missing), missing, translated, strict=True)),
    )
    conn.commit()
