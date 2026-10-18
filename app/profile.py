import csv
import io
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from app import i18n
from app.auth import CurrentUser
from app.bans import BAN_CATEGORIES, KNOWN_CATEGORY_IDS
from app.db import Conn

# Canonical DK chains from ShelfAtlas: (slug, display name).
CHAINS: list[tuple[str, str]] = [
    ("aldi", "Aldi"),
    ("bilka", "Bilka"),
    ("bordershop", "BorderShop"),
    ("calle", "Calle"),
    ("citti", "CITTI"),
    ("coop-365discount", "Coop 365discount"),
    ("dagli-brugsen", "Dagli'Brugsen"),
    ("fakta-tyskland", "fakta Tyskland"),
    ("fleggaard", "Fleggaard"),
    ("foetex", "Føtex"),
    ("irma", "Irma"),
    ("kvickly", "Kvickly"),
    ("let-koeb", "LET-KØB"),
    ("lidl", "Lidl"),
    ("loevbjerg", "Løvbjerg"),
    ("meny", "Meny"),
    ("min-koebmand", "Min Købmand"),
    ("netto", "Netto"),
    ("poetzsch", "Pøtzsch"),
    ("rema", "Rema 1000"),
    ("scandinavian-park", "Scandinavian Park"),
    ("spar", "Spar"),
    ("superbrugsen", "SuperBrugsen"),
]
_CHAIN_SLUGS = {slug for slug, _ in CHAINS}


@dataclass
class Preferences:
    max_kcal: int | None
    min_protein_g: int | None
    restrictions: str
    servings: int
    stores: list[str]
    bans: list[str] = field(default_factory=list)  # banned category ids + custom items
    language: str = ""  # display language for recipes/shopping; "" = English, no translation


def selected_store_slugs(conn: psycopg.Connection, user_id: int) -> list[str]:
    """The user's store whitelist — the single source used by offers/shopping."""
    return [r["chain_slug"] for r in conn.execute(
        "SELECT chain_slug FROM user_stores WHERE user_id = %s ORDER BY chain_slug", (user_id,))]


def load_preferences(conn: psycopg.Connection, user_id: int) -> Preferences:
    row = conn.execute(
        "SELECT max_kcal, min_protein_g, restrictions, servings, bans, language "
        "FROM user_preferences WHERE user_id = %s", (user_id,)
    ).fetchone()
    stores = selected_store_slugs(conn, user_id)
    if row is None:
        return Preferences(None, None, "", 2, stores, [], "")
    return Preferences(
        row["max_kcal"], row["min_protein_g"], row["restrictions"], row["servings"],
        stores, json.loads(row["bans"]), row["language"],
    )


def save_preferences(conn: psycopg.Connection, user_id: int, prefs: Preferences) -> None:
    with conn.transaction():  # preferences + store whitelist change together or not at all
        _save_preferences(conn, user_id, prefs)


def _save_preferences(conn: psycopg.Connection, user_id: int, prefs: Preferences) -> None:
    conn.execute(
        """
        INSERT INTO user_preferences
            (user_id, max_kcal, min_protein_g, restrictions, servings, bans, language)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(user_id) DO UPDATE SET
            max_kcal = excluded.max_kcal,
            min_protein_g = excluded.min_protein_g,
            restrictions = excluded.restrictions,
            servings = excluded.servings,
            bans = excluded.bans,
            language = excluded.language
        """,
        (user_id, prefs.max_kcal, prefs.min_protein_g, prefs.restrictions, prefs.servings,
         json.dumps(prefs.bans), prefs.language),
    )
    conn.execute("DELETE FROM user_stores WHERE user_id = %s", (user_id,))
    conn.cursor().executemany(
        "INSERT INTO user_stores (user_id, chain_slug) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        [(user_id, s) for s in prefs.stores if s in _CHAIN_SLUGS],
    )


# --- ratings / preference learning (the "user view over time") ---


@dataclass
class Rating:
    id: int
    title: str
    score: int
    tags: list[str]
    created_at: str


def save_rating(conn: psycopg.Connection, user_id: int, title: str, score: int,
                tags: list[str]) -> None:
    conn.execute(
        "INSERT INTO ratings (user_id, title, score, tags, created_at) VALUES (%s, %s, %s, %s, %s)",
        (user_id, title, score, json.dumps(tags), datetime.now(UTC)),
    )


def rating_history(conn: psycopg.Connection, user_id: int) -> list[Rating]:
    return [
        Rating(r["id"], r["title"], r["score"], json.loads(r["tags"]), r["created_at"].isoformat())
        for r in conn.execute(
            "SELECT * FROM ratings WHERE user_id = %s ORDER BY id DESC", (user_id,))
    ]


def preference_summary(conn: psycopg.Connection, user_id: int) -> str:
    """Aggregate ratings into a liked/avoid ingredient signal fed to the agent.

    Net score per ingredient tag across all ratings; no ML, no vector DB — just SQL-ish
    aggregation that generalizes preferences across weeks.
    """
    net: dict[str, int] = {}
    for row in conn.execute(
            "SELECT score, tags FROM ratings WHERE user_id = %s ORDER BY id", (user_id,)):
        for tag in json.loads(row["tags"]):
            net[tag] = net.get(tag, 0) + row["score"]
    liked = [t for t, s in sorted(net.items(), key=lambda kv: -kv[1]) if s > 0][:6]
    disliked = [t for t, s in sorted(net.items(), key=lambda kv: kv[1]) if s < 0][:6]
    parts = []
    if liked:
        parts.append("Prefers: " + ", ".join(liked) + ".")
    if disliked:
        parts.append("Avoid: " + ", ".join(disliked) + ".")
    return " ".join(parts)


# --- full data export (everything the app stores about the user) ---


def export_all(conn: psycopg.Connection, user_id: int) -> dict:
    plans = []
    for p in conn.execute(
            "SELECT id, created_at, week FROM plans WHERE user_id = %s ORDER BY id", (user_id,)):
        recipes = []
        for r in conn.execute(
            "SELECT * FROM recipes WHERE plan_id = %s ORDER BY slot_index", (p["id"],)
        ):
            ingredients = [
                {"name": i["name"], "grams": i["grams"], "name_translated": i["name_translated"]}
                for i in conn.execute(
                    "SELECT name, grams, name_translated FROM recipe_ingredients "
                    "WHERE recipe_id = %s ORDER BY id",
                    (r["id"],),
                )
            ]
            recipes.append({
                "title": r["title"],
                "title_translated": r["title_translated"],
                "servings": r["servings"],
                "steps": json.loads(r["steps"]),
                "steps_translated": json.loads(r["steps_translated"]) if r["steps_translated"] else None,
                "per_serving": {
                    "kcal": r["per_serving_kcal"], "protein_g": r["per_serving_protein_g"],
                    "carbs_g": r["per_serving_carbs_g"], "fat_g": r["per_serving_fat_g"],
                    "fibre_g": r["per_serving_fibre_g"],
                },
                "flagged": bool(r["flagged"]),
                "ingredients": ingredients,
            })
        plans.append({"id": p["id"], "created_at": p["created_at"].isoformat(), "week": p["week"],
                      "recipes": recipes})
    missing = [
        {**r, "last_seen": r["last_seen"].isoformat()} for r in conn.execute(
            "SELECT name, hits, caused_revise, sample_grams, last_seen FROM missing_ingredients "
            "ORDER BY caused_revise DESC, hits DESC, name"
        )
    ]
    return {
        "preferences": asdict(load_preferences(conn, user_id)),
        "plans": plans,
        "ratings": [asdict(r) for r in rating_history(conn, user_id)],
        "missing_ingredients": missing,
    }


def ratings_csv(conn: psycopg.Connection, user_id: int) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["created_at", "title", "score", "tags"])
    for r in rating_history(conn, user_id):
        writer.writerow([r.created_at, r.title, r.score, ", ".join(r.tags)])
    return buf.getvalue()


router = APIRouter()


@router.get("/profile", response_class=HTMLResponse)
def profile_form(request: Request, conn: Conn, user: CurrentUser, saved: bool = False):
    prefs = load_preferences(conn, user.id)
    custom_bans = [b for b in prefs.bans if b not in KNOWN_CATEGORY_IDS]
    return request.app.state.templates.TemplateResponse(
        request,
        "profile.html",
        {"title": "Preferences", "prefs": prefs, "chains": CHAINS,
         "ban_categories": BAN_CATEGORIES, "custom_bans": custom_bans, "saved": saved},
    )


@router.post("/profile")
def profile_save(
    request: Request,
    conn: Conn,
    user: CurrentUser,
    max_kcal: Annotated[str, Form()] = "",
    min_protein_g: Annotated[str, Form()] = "",
    restrictions: Annotated[str, Form()] = "",
    servings: Annotated[int, Form()] = 2,
    stores: Annotated[list[str] | None, Form()] = None,
    bans: Annotated[list[str] | None, Form()] = None,
    bans_custom: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = "",
):
    # kept chips (categories + existing customs) + newly typed customs; replaces the whole list
    kept = [b.strip().lower() for b in (bans or []) if b.strip()]
    added = [b.strip().lower() for b in bans_custom.split(",") if b.strip()]
    all_bans = list(dict.fromkeys([*kept, *added]))  # dedupe, preserve order
    language = language.strip()
    save_preferences(
        conn,
        user.id,
        Preferences(
            max_kcal=int(max_kcal) if max_kcal.strip() else None,
            min_protein_g=int(min_protein_g) if min_protein_g.strip() else None,
            restrictions=restrictions.strip(),
            servings=servings,
            stores=stores or [],
            bans=all_bans,
            language=language,
        ),
    )
    if language:
        # warm the static UI-chrome cache (app/i18n.py) so nav/headings show translated too,
        # not just recipes — best-effort, never blocks the save (see i18n.warm docstring)
        services = request.app.state.build_services(request.app.state.config, conn)
        i18n.warm(conn, language, services.translator)
    return RedirectResponse("/profile?saved=1", status_code=303)


@router.get("/me/export")
def export_profile(conn: Conn, user: CurrentUser):
    return JSONResponse(asdict(load_preferences(conn, user.id)))


@router.get("/me", response_class=HTMLResponse)
def my_data(request: Request, conn: Conn, user: CurrentUser):
    return request.app.state.templates.TemplateResponse(
        request, "me.html", {"title": "My data", "ratings": rating_history(conn, user.id)}
    )


@router.get("/me/all.json")
def export_all_json(conn: Conn, user: CurrentUser):
    return JSONResponse(export_all(conn, user.id))


@router.get("/me/ratings.csv")
def export_ratings_csv(conn: Conn, user: CurrentUser):
    return Response(
        ratings_csv(conn, user.id),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ratings.csv"},
    )

