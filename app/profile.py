import csv
import io
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

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


def selected_store_slugs(conn: sqlite3.Connection) -> list[str]:
    """The user's store whitelist — the single source used by offers/shopping."""
    return [r["chain_slug"] for r in conn.execute("SELECT chain_slug FROM stores ORDER BY chain_slug")]


def load_preferences(conn: sqlite3.Connection) -> Preferences:
    row = conn.execute(
        "SELECT max_kcal, min_protein_g, restrictions, servings, bans FROM preferences WHERE id = 1"
    ).fetchone()
    stores = selected_store_slugs(conn)
    if row is None:
        return Preferences(None, None, "", 2, stores, [])
    return Preferences(
        row["max_kcal"], row["min_protein_g"], row["restrictions"], row["servings"],
        stores, json.loads(row["bans"]),
    )


def save_preferences(conn: sqlite3.Connection, prefs: Preferences) -> None:
    conn.execute(
        """
        INSERT INTO preferences (id, max_kcal, min_protein_g, restrictions, servings, bans)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            max_kcal = excluded.max_kcal,
            min_protein_g = excluded.min_protein_g,
            restrictions = excluded.restrictions,
            servings = excluded.servings,
            bans = excluded.bans
        """,
        (prefs.max_kcal, prefs.min_protein_g, prefs.restrictions, prefs.servings,
         json.dumps(prefs.bans)),
    )
    conn.execute("DELETE FROM stores")
    conn.executemany(
        "INSERT INTO stores (chain_slug) VALUES (?)",
        [(s,) for s in prefs.stores if s in _CHAIN_SLUGS],
    )
    conn.commit()


# --- ratings / preference learning (the "user view over time") ---


@dataclass
class Rating:
    id: int
    title: str
    score: int
    tags: list[str]
    created_at: str


def save_rating(conn: sqlite3.Connection, title: str, score: int, tags: list[str]) -> None:
    conn.execute(
        "INSERT INTO ratings (title, score, tags, created_at) VALUES (?, ?, ?, ?)",
        (title, score, json.dumps(tags), datetime.now(UTC).isoformat()),
    )
    conn.commit()


def rating_history(conn: sqlite3.Connection) -> list[Rating]:
    return [
        Rating(r["id"], r["title"], r["score"], json.loads(r["tags"]), r["created_at"])
        for r in conn.execute("SELECT * FROM ratings ORDER BY id DESC")
    ]


def preference_summary(conn: sqlite3.Connection) -> str:
    """Aggregate ratings into a liked/avoid ingredient signal fed to the agent.

    Net score per ingredient tag across all ratings; no ML, no vector DB — just SQL-ish
    aggregation that generalizes preferences across weeks.
    """
    net: dict[str, int] = {}
    for row in conn.execute("SELECT score, tags FROM ratings"):
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


def export_all(conn: sqlite3.Connection) -> dict:
    plans = []
    for p in conn.execute("SELECT id, created_at, week FROM plans ORDER BY id"):
        recipes = []
        for r in conn.execute(
            "SELECT * FROM recipes WHERE plan_id = ? ORDER BY slot_index", (p["id"],)
        ):
            ingredients = [
                {"name": i["name"], "grams": i["grams"]}
                for i in conn.execute(
                    "SELECT name, grams FROM recipe_ingredients WHERE recipe_id = ? ORDER BY id",
                    (r["id"],),
                )
            ]
            recipes.append({
                "title": r["title"],
                "servings": r["servings"],
                "steps": json.loads(r["steps"]),
                "per_serving": {
                    "kcal": r["per_serving_kcal"], "protein_g": r["per_serving_protein_g"],
                    "carbs_g": r["per_serving_carbs_g"], "fat_g": r["per_serving_fat_g"],
                    "fibre_g": r["per_serving_fibre_g"],
                },
                "flagged": bool(r["flagged"]),
                "ingredients": ingredients,
            })
        plans.append({"id": p["id"], "created_at": p["created_at"], "week": p["week"],
                      "recipes": recipes})
    missing = [
        dict(r) for r in conn.execute(
            "SELECT name, hits, caused_revise, sample_grams, last_seen FROM missing_ingredients "
            "ORDER BY caused_revise DESC, hits DESC"
        )
    ]
    return {
        "preferences": asdict(load_preferences(conn)),
        "plans": plans,
        "ratings": [asdict(r) for r in rating_history(conn)],
        "missing_ingredients": missing,
    }


def ratings_csv(conn: sqlite3.Connection) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["created_at", "title", "score", "tags"])
    for r in rating_history(conn):
        writer.writerow([r.created_at, r.title, r.score, ", ".join(r.tags)])
    return buf.getvalue()


router = APIRouter()


@router.get("/profile", response_class=HTMLResponse)
def profile_form(request: Request, conn: Conn):
    prefs = load_preferences(conn)
    custom_bans = [b for b in prefs.bans if b not in KNOWN_CATEGORY_IDS]
    return request.app.state.templates.TemplateResponse(
        request,
        "profile.html",
        {"title": "Preferences", "prefs": prefs, "chains": CHAINS,
         "ban_categories": BAN_CATEGORIES, "custom_bans": custom_bans},
    )


@router.post("/profile")
def profile_save(
    conn: Conn,
    max_kcal: Annotated[str, Form()] = "",
    min_protein_g: Annotated[str, Form()] = "",
    restrictions: Annotated[str, Form()] = "",
    servings: Annotated[int, Form()] = 2,
    stores: Annotated[list[str] | None, Form()] = None,
    bans: Annotated[list[str] | None, Form()] = None,
    bans_custom: Annotated[str, Form()] = "",
):
    # kept chips (categories + existing customs) + newly typed customs; replaces the whole list
    kept = [b.strip().lower() for b in (bans or []) if b.strip()]
    added = [b.strip().lower() for b in bans_custom.split(",") if b.strip()]
    all_bans = list(dict.fromkeys([*kept, *added]))  # dedupe, preserve order
    save_preferences(
        conn,
        Preferences(
            max_kcal=int(max_kcal) if max_kcal.strip() else None,
            min_protein_g=int(min_protein_g) if min_protein_g.strip() else None,
            restrictions=restrictions.strip(),
            servings=servings,
            stores=stores or [],
            bans=all_bans,
        ),
    )
    return RedirectResponse("/profile", status_code=303)


@router.get("/me/export")
def export_profile(conn: Conn):
    return JSONResponse(asdict(load_preferences(conn)))


@router.get("/me", response_class=HTMLResponse)
def my_data(request: Request, conn: Conn):
    return request.app.state.templates.TemplateResponse(
        request, "me.html", {"title": "My data", "ratings": rating_history(conn)}
    )


@router.get("/me/all.json")
def export_all_json(conn: Conn):
    return JSONResponse(export_all(conn))


@router.get("/me/ratings.csv")
def export_ratings_csv(conn: Conn):
    return Response(
        ratings_csv(conn),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ratings.csv"},
    )


@router.get("/me/database.db")
def export_database(request: Request):
    return FileResponse(
        request.app.state.config.db_path,
        media_type="application/octet-stream",
        filename="food_service.db",
    )
