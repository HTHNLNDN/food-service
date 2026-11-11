"""Onboarding: a short guided setup right after sign-up (welcome, five questions, review).

The `user_onboarding` row holds the state and the resume point. It's created at sign-up
only, together with the default preferences, so skipping needs no special case and
accounts that existed before never see onboarding. Answers land in the normal per-user
preferences; the row doesn't store them twice.
"""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import conditions, goals
from app.auth import CurrentUser
from app.bans import ALLERGY_CHIPS, BAN_CATEGORIES, DIET_CHIPS, DISLIKES, ban_display
from app.db import Conn
from app.profile import Preferences, load_preferences, parse_bans, save_preferences

STEPS = ("welcome", "household", "goals", "leave_out", "cooking", "stores", "review")
QUESTIONS = STEPS[1:-1]  # "Step n of 5"
# Built so far. Unbuilt steps are passed over (GET redirects to the next built one).
BUILT = ("welcome", "household", "goals", "leave_out", "cooking", "review")

DEFAULT_SERVINGS = 2
MIN_SERVINGS, MAX_SERVINGS = 1, 8
COOKING_CHIPS = ("No deep-frying", "No whole fish or offal", "Short ingredient lists",
                 "No seed oils", "One-pot or sheet-pan")


@dataclass(frozen=True)
class Onboarding:
    started_at: datetime
    completed_at: datetime | None
    skipped_at: datetime | None
    dismissed_at: datetime | None
    step: str

    @property
    def active(self) -> bool:
        """Not finished and not skipped: the guided setup can still be continued."""
        return self.completed_at is None and self.skipped_at is None

    @property
    def steps_left(self) -> int:
        """Built screens from the resume point through the review (the welcome doesn't count)."""
        start = max(STEPS.index(self.step), 1)
        return sum(1 for s in STEPS[start:] if s in BUILT)


@dataclass(frozen=True)
class Answers:
    """Onboarding answers that don't live in the preferences: what the planner reads."""
    goals: list[str]
    conditions: list[str]
    cooking_style: str


def answers(conn: psycopg.Connection, user_id: int) -> Answers:
    row = conn.execute("SELECT goals, conditions, cooking_style FROM user_onboarding "
                       "WHERE user_id = %s", (user_id,)).fetchone()
    if row is None:
        return Answers([], [], "")
    return Answers(json.loads(row["goals"]), json.loads(row["conditions"]), row["cooking_style"])


def save_cooking_style(conn: psycopg.Connection, user_id: int, text: str) -> None:
    conn.execute("UPDATE user_onboarding SET cooking_style = %s WHERE user_id = %s",
                 (text, user_id))


def save_goals(conn: psycopg.Connection, user_id: int, goal_ids: list[str],
               condition_ids: list[str]) -> None:
    """Remember the goal and condition ids, and set the targets the goals produce."""
    kcal, protein = goals.targets_for(goal_ids)
    with conn.transaction():
        conn.execute("UPDATE user_onboarding SET goals = %s, conditions = %s WHERE user_id = %s",
                     (json.dumps(goal_ids), json.dumps(condition_ids), user_id))
        prefs = load_preferences(conn, user_id)
        save_preferences(conn, user_id, replace(prefs, max_kcal=kcal, min_protein_g=protein))


def next_step(step: str) -> str:
    """The next built step after `step`; the review is the last."""
    for s in STEPS[STEPS.index(step) + 1:]:
        if s in BUILT:
            return s
    return "review"


def start(conn: psycopg.Connection, user_id: int) -> None:
    """Begin onboarding for a brand-new account: save the defaults and a welcome row."""
    kcal, protein = goals.DEFAULT_TARGETS
    with conn.transaction():
        save_preferences(conn, user_id, Preferences(
            max_kcal=kcal, min_protein_g=protein, restrictions="",
            servings=DEFAULT_SERVINGS, stores=[], bans=[], language=""))
        conn.execute("INSERT INTO user_onboarding (user_id, started_at, step) "
                     "VALUES (%s, %s, 'welcome')", (user_id, datetime.now(UTC)))


def load(conn: psycopg.Connection, user_id: int) -> Onboarding | None:
    row = conn.execute(
        "SELECT started_at, completed_at, skipped_at, dismissed_at, step "
        "FROM user_onboarding WHERE user_id = %s", (user_id,)).fetchone()
    return Onboarding(**row) if row else None


def advance(conn: psycopg.Connection, user_id: int, from_step: str) -> str:
    """Move the resume point past `from_step`; returns where the user goes next."""
    step = next_step(from_step)
    conn.execute("UPDATE user_onboarding SET step = %s WHERE user_id = %s", (step, user_id))
    return step


def _stamp(conn: psycopg.Connection, user_id: int, column: str) -> None:
    # `column` is one of three fixed names below, never user input
    conn.execute(f"UPDATE user_onboarding SET {column} = COALESCE({column}, %s) "
                 "WHERE user_id = %s", (datetime.now(UTC), user_id))


def skip_all(conn: psycopg.Connection, user_id: int) -> None:
    _stamp(conn, user_id, "skipped_at")


def complete(conn: psycopg.Connection, user_id: int) -> None:
    _stamp(conn, user_id, "completed_at")


def dismiss(conn: psycopg.Connection, user_id: int) -> None:
    _stamp(conn, user_id, "dismissed_at")


def card_steps_left(conn: psycopg.Connection, user_id: int) -> int | None:
    """Steps left for the plan page's "Finish setting up" card, or None for no card."""
    ob = load(conn, user_id)
    if ob is None or not ob.active or ob.dismissed_at is not None:
        return None
    return ob.steps_left


# --- routes ---

router = APIRouter()

TITLES = {"welcome": "Welcome", "household": "Your household", "goals": "Your goals",
          "leave_out": "Foods to leave out", "cooking": "How you like to cook",
          "review": "Your setup"}


def _home() -> RedirectResponse:
    return RedirectResponse("/", status_code=303)


def _to(step: str) -> RedirectResponse:
    return RedirectResponse(f"/onboarding/{step}", status_code=303)


def _known(step: str) -> None:
    if step not in STEPS:
        raise HTTPException(status_code=404)


def _screen(request: Request, conn: psycopg.Connection, user_id: int, step: str,
            status_code: int = 200, **ctx):
    number = QUESTIONS.index(step) + 1 if step in QUESTIONS else None
    prefs = load_preferences(conn, user_id)
    categories = {cid: display for cid, display, _ in BAN_CATEGORIES}
    known = set(categories) | {did for did, _, _ in DISLIKES}
    saved = answers(conn, user_id)
    style = saved.cooking_style
    parts = [p.strip() for p in style.split(";") if p.strip()]
    by_goal = {g.id: g for g in goals.GOALS}
    by_condition = {c.id: c for c in conditions.CONDITIONS}
    return request.app.state.templates.TemplateResponse(
        request, f"onboarding/{step}.html",
        {"title": TITLES[step], "step": step, "number": number, "total": len(QUESTIONS),
         "prefs": prefs, "ban_names": ban_display(prefs.bans),
         "diet_chips": [(c, categories[c]) for c in DIET_CHIPS],
         "allergy_chips": [(c, categories[c]) for c in ALLERGY_CHIPS],
         "dislike_chips": [(d, display) for d, display, _ in DISLIKES],
         "custom_bans": [b for b in prefs.bans if b not in known],
         "cooking_chips": COOKING_CHIPS, "cooking_style": style,
         "cooking_picked": [p for p in parts if p in COOKING_CHIPS],
         "cooking_note": "; ".join(p for p in parts if p not in COOKING_CHIPS),
         "goal_options": goals.GOALS, "max_goals": goals.MAX_GOALS,
         "condition_options": conditions.CONDITIONS,
         "picked_goals": [by_goal[i] for i in goals.known(saved.goals)],
         "picked_conditions": [by_condition[i] for i in conditions.known(saved.conditions)],
         **ctx},
        status_code=status_code,
    )


@router.get("/onboarding")
def resume(conn: Conn, user: CurrentUser):
    ob = load(conn, user.id)
    return _to(ob.step) if ob is not None and ob.active else _home()


@router.get("/onboarding/{step}")
def show_step(step: str, request: Request, conn: Conn, user: CurrentUser):
    _known(step)
    ob = load(conn, user.id)
    if ob is None or not ob.active:
        return _home()
    if step not in BUILT:
        return _to(next_step(step))
    return _screen(request, conn, user.id, step)


@router.post("/onboarding/dismiss")  # before the step POST, so "dismiss" isn't read as a step
def dismiss_card(conn: Conn, user: CurrentUser):
    dismiss(conn, user.id)
    return _home()


@router.post("/onboarding/{step}")
def answer_step(
    step: str,
    request: Request,
    conn: Conn,
    user: CurrentUser,
    action: Annotated[str, Form()] = "next",
    servings: Annotated[str, Form()] = "",
    bans: Annotated[list[str] | None, Form()] = None,
    bans_custom: Annotated[str, Form()] = "",
    cooking: Annotated[list[str] | None, Form()] = None,
    cooking_note: Annotated[str, Form()] = "",
    # trailing underscore: `goals` and `conditions` are the modules
    goals_: Annotated[list[str] | None, Form(alias="goals")] = None,
    conditions_: Annotated[list[str] | None, Form(alias="conditions")] = None,
):
    _known(step)
    ob = load(conn, user.id)
    if ob is None or not ob.active or step not in BUILT:
        return _home()
    if action == "skip_all":
        skip_all(conn, user.id)
        return _home()
    if step == "review":
        if action == "done":
            complete(conn, user.id)
            return _home()
        return _to("review")
    if step == "household" and action == "next":
        n = int(servings) if servings.strip().lstrip("-").isdigit() else 0
        if not MIN_SERVINGS <= n <= MAX_SERVINGS:
            return _screen(request, conn, user.id, step, status_code=422,
                           error=f"Pick a number between {MIN_SERVINGS} and {MAX_SERVINGS}.")
        prefs = load_preferences(conn, user.id)
        save_preferences(conn, user.id, replace(prefs, servings=n))
    if step == "goals":
        picked = goals.known(goals_ or []) if action == "next" else []
        if len(picked) > goals.MAX_GOALS:
            return _screen(request, conn, user.id, step, status_code=422,
                           error=f"Pick up to {goals.MAX_GOALS} goals.")
        chosen = conditions.known(conditions_ or []) if action == "next" else []
        save_goals(conn, user.id, picked, chosen)  # skip: balanced defaults, no condition
    if step == "leave_out" and action == "next":
        prefs = load_preferences(conn, user.id)
        save_preferences(conn, user.id, replace(prefs, bans=parse_bans(bans, bans_custom)))
    if step == "cooking":  # skipping clears it: no preference
        picked = [c for c in COOKING_CHIPS if c in (cooking or [])] if action == "next" else []
        note = cooking_note.strip() if action == "next" else ""
        save_cooking_style(conn, user.id, "; ".join([*picked, *([note] if note else [])]))
    return _to(advance(conn, user.id, step))  # "next" or "skip"
