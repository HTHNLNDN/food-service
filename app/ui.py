import sqlite3
import threading
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.dates import iso_week
from app.db import Conn, connect
from app.planner import decline, generate_plan, load_latest_plan, weekday_meals
from app.profile import save_rating, selected_store_slugs
from app.shopping import build_shopping_list

router = APIRouter()

# Planning is slow (~1-2 min of grounded LLM + macro checks), which exceeds a phone browser's
# request timeout. So POST /plan starts the work in the background and returns immediately; the
# page shows "Planning…" and auto-refreshes until it's ready. Single household -> one job, tracked
# in module state. Tests set app.state.plan_sync to run inline for determinism.
_plan_lock = threading.Lock()
_plan_state: dict[str, object] = {"running": False, "error": None}


def _friendly(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:400]


def _run_job(config, build, work) -> None:
    error = None
    try:
        conn = connect(config.db_path)  # the thread needs its own connection
        try:
            work(conn, build(config, conn))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - surface planning failures in the UI, never as a 500
        error = _friendly(exc)
    with _plan_lock:
        _plan_state["error"] = error
        _plan_state["running"] = False


def _start_job(request: Request, work) -> None:
    """Run `work(conn, services)` in the background (or inline when app.state.plan_sync)."""
    with _plan_lock:
        if _plan_state["running"]:
            return  # a job is already in flight — ignore the duplicate submit
        _plan_state["running"] = True
        _plan_state["error"] = None
    config = request.app.state.config
    build = request.app.state.build_services
    if getattr(request.app.state, "plan_sync", False):
        _run_job(config, build, work)  # deterministic path for tests
    else:
        threading.Thread(target=_run_job, args=(config, build, work), daemon=True).start()


def _shopping_for(request: Request, conn: sqlite3.Connection, plan):
    selected = selected_store_slugs(conn)
    if not (plan and selected):
        return None
    try:
        services = request.app.state.build_services(request.app.state.config, conn)
        return build_shopping_list(
            plan.recipes, services.offers.offers(selected), selected,
            matcher=services.offer_matcher, cache=conn, week=iso_week(datetime.now(UTC).date()),
            have=plan.use_up,
        )
    except Exception:  # noqa: BLE001 - a shopping/offers hiccup must not break the page
        return None


def _render_home(request: Request, conn: sqlite3.Connection):
    with _plan_lock:
        planning = _plan_state["running"]
        error = _plan_state["error"]
    config = request.app.state.config
    plan = load_latest_plan(conn)
    return request.app.state.templates.TemplateResponse(
        request,
        "plan.html",
        {
            "title": "This week",
            "plan": plan,
            "meals": weekday_meals(plan.recipes) if plan else [],
            "llm_ready": bool(config.llm_base_url and config.llm_model),
            "planning": planning,
            "error": error,
        },
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request, conn: Conn):
    return _render_home(request, conn)


@router.get("/print", response_class=HTMLResponse)
def print_recipes(request: Request, conn: Conn):
    return request.app.state.templates.TemplateResponse(
        request, "print.html", {"title": "Recipes", "plan": load_latest_plan(conn)}
    )


@router.get("/cost", response_class=HTMLResponse)
def cost(request: Request, conn: Conn):
    by_type = conn.execute(
        "SELECT call_type, model, COUNT(*) n, COALESCE(SUM(input_tokens),0) in_tok, "
        "COALESCE(SUM(output_tokens),0) out_tok, COALESCE(SUM(search_queries),0) sq, "
        "SUM(usd) usd, COALESCE(SUM(retries),0) retries, "
        "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) errs, AVG(latency_ms) avg_ms "
        "FROM v_llm_cost GROUP BY call_type, model ORDER BY usd DESC"
    ).fetchall()
    totals = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(usd),0) usd FROM v_llm_cost").fetchone()
    plans = conn.execute(
        "SELECT p.id, p.created_at, "
        "(SELECT COALESCE(SUM(usd),0) FROM v_llm_cost c WHERE c.plan_id = p.id) usd, "
        "(SELECT COUNT(*) FROM llm_calls c WHERE c.plan_id = p.id AND c.call_type='replace') revises, "
        "(SELECT COUNT(*) FROM recipes r WHERE r.plan_id = p.id) dinners, "
        "(SELECT COALESCE(SUM(flagged),0) FROM recipes r WHERE r.plan_id = p.id) flagged "
        "FROM plans p ORDER BY p.id DESC LIMIT 10"
    ).fetchall()
    unpriced = [r["model"] for r in conn.execute(
        "SELECT DISTINCT model FROM llm_calls WHERE model NOT IN (SELECT model FROM model_prices)"
    )]
    return request.app.state.templates.TemplateResponse(
        request, "cost.html",
        {"title": "Cost", "by_type": by_type, "totals": totals, "plans": plans,
         "unpriced": unpriced, "trends": _weekly_trends(conn)},
    )


def _weekly_trends(conn: sqlite3.Connection) -> list[dict]:
    """Efficacy over time, per ISO week — derived from stored plans/recipes/traces, no new spend."""
    weeks = conn.execute(
        "SELECT p.week week, COUNT(DISTINCT p.id) plans, COUNT(r.id) dinners, "
        "COALESCE(SUM(r.flagged), 0) flagged "
        "FROM plans p LEFT JOIN recipes r ON r.plan_id = p.id "
        "GROUP BY p.week ORDER BY p.week DESC LIMIT 8"
    ).fetchall()
    cost = {row["week"]: row for row in conn.execute(
        "SELECT p.week week, COALESCE(SUM(v.usd), 0) usd, "
        "SUM(CASE WHEN v.call_type = 'replace' THEN 1 ELSE 0 END) revises "
        "FROM plans p JOIN v_llm_cost v ON v.plan_id = p.id GROUP BY p.week"
    )}
    out = []
    for w in weeks:
        c = cost.get(w["week"])
        plans, dinners = w["plans"] or 1, w["dinners"] or 1
        out.append({
            "week": w["week"], "plans": w["plans"], "dinners": w["dinners"],
            "off_target": 100 * w["flagged"] / dinners,
            "revises_per_plan": (c["revises"] if c else 0) / plans,
            "usd_per_plan": (c["usd"] if c else 0.0) / plans,
        })
    return out


@router.get("/shopping", response_class=HTMLResponse)
def shopping(request: Request, conn: Conn):
    plan = load_latest_plan(conn)
    return request.app.state.templates.TemplateResponse(
        request,
        "shopping.html",
        {"title": "Shopping", "plan": plan, "shopping": _shopping_for(request, conn, plan)},
    )


@router.post("/plan")
def make_plan(request: Request, use_up: Annotated[str, Form()] = ""):
    text = use_up.strip()
    _start_job(request, lambda conn, svc: generate_plan(
        conn, agent=svc.agent, offers=svc.offers, nutrition=svc.nutrition, use_up=text))
    return RedirectResponse("/", status_code=303)


@router.post("/plan/{slot}/decline")
def decline_dish(request: Request, conn: Conn, slot: int):
    plan = load_latest_plan(conn)
    if plan is not None and 0 <= slot < len(plan.recipes):
        pid = plan.id
        _start_job(request, lambda c, svc: decline(
            c, pid, slot, agent=svc.agent, offers=svc.offers, nutrition=svc.nutrition))
    return RedirectResponse("/", status_code=303)


@router.post("/plan/{slot}/rate")
def rate_dish(request: Request, conn: Conn, slot: int, score: Annotated[int, Form()] = 0):
    plan = load_latest_plan(conn)
    if plan is not None and 0 <= slot < len(plan.recipes):
        r = plan.recipes[slot]
        save_rating(conn, r.title, score, [i.name for i in r.ingredients])
    return RedirectResponse("/", status_code=303)
