"""Offline eval: run the real pipeline against golden scenarios, score deterministically, and
report cost + quality. Results persist to <data_dir>/evals.db (eval_runs/eval_scores) so runs
compare over time.

    uv run python evals/run.py                     # all scenarios, 1 sample each
    uv run python evals/run.py --samples 3         # 3 samples/scenario (mean reported)
    uv run python evals/run.py --scenario no_fish  # one scenario

Makes REAL LLM calls (~$0.01 + ~45s per run). A full sweep is ~$0.12 (or ~$0.36 at 3 samples).
"""

import argparse
import os
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import telemetry
from app.agent import GeminiGroundedAgent, OpenAICompatibleAgent
from app.config import load_config
from app.db import bootstrap, connect
from app.nutrition import LLMCandidatePicker, LocalNutritionSource
from app.offers import Offer
from app.planner import generate_plan
from app.profile import Preferences, save_preferences
from app.services import Services, build_services
from evals import scorers
from evals.judge import AXES, LLMJudge
from evals.models import candidates
from evals.scenarios import SCENARIOS

_BASE_METRICS = ["macro_pass", "ban_violations", "flagged", "proteins", "repetition",
                 "usd", "revises", "usd_per_dinner", "latency_s"]
_LABELS = {"macro_pass": "macro_pass", "ban_violations": "bans", "flagged": "flagged",
           "proteins": "proteins", "repetition": "repetition", "usd": "usd",
           "revises": "revises", "usd_per_dinner": "$/dinner", "latency_s": "latency_s",
           "appeal": "appeal", "realism": "realism", "effort": "effort"}


class FixedOffers:
    """A pinned OffersProvider so eval runs are comparable (no live ShelfAtlas)."""

    def __init__(self, names, chain="rema"):
        self._offers = [Offer(chain, n, Decimal(10), "DKK", "", "", None, None, None, "campaign")
                        for n in names]

    def offers(self, selected):
        allowed = set(selected)
        return [o for o in self._offers if o.chain_slug in allowed]


def _seed_history(conn, titles):
    if not titles:
        return
    cur = conn.execute("INSERT INTO plans (created_at, week, use_up) VALUES (?, '2026-W01', '')",
                       (datetime.now(UTC).isoformat(),))
    pid = cur.lastrowid
    for slot, title in enumerate(titles):
        conn.execute("INSERT INTO recipes (plan_id, slot_index, title, servings, steps) "
                     "VALUES (?, ?, ?, 2, '[]')", (pid, slot, title))
    conn.commit()


def run_scenario(config, scn, samples, build=build_services, judge=None):
    """Return a list of per-sample metric dicts (one real plan each)."""
    out = []
    for _ in range(samples):
        db = Path(tempfile.mkdtemp()) / "eval.db"
        conn = connect(db)
        bootstrap(conn)
        save_preferences(conn, Preferences(scn.max_kcal, scn.min_protein_g, scn.restrictions,
                                           scn.servings, list(scn.stores), list(scn.bans)))
        _seed_history(conn, scn.seed_history)
        telemetry.configure(db)  # cost for this run lands in the scratch db
        services = build(config, conn)
        plan = generate_plan(conn, agent=services.agent, offers=FixedOffers(scn.offers),
                             nutrition=services.nutrition, num_dinners=scn.num_dinners,
                             use_up=scn.use_up)
        r = plan.recipes
        usd = conn.execute("SELECT COALESCE(SUM(usd),0) u FROM v_llm_cost WHERE plan_id=?",
                           (plan.id,)).fetchone()["u"]
        revises = conn.execute("SELECT COUNT(*) n FROM llm_calls WHERE plan_id=? AND call_type='replace'",
                               (plan.id,)).fetchone()["n"]
        gen_ms = conn.execute("SELECT COALESCE(SUM(latency_ms),0) ms FROM llm_calls "
                              "WHERE plan_id=? AND call_type='plan_week'", (plan.id,)).fetchone()["ms"]
        dinners = len(r) or 1
        total_usd = conn.execute("SELECT COALESCE(SUM(usd),0) u FROM v_llm_cost").fetchone()["u"]
        sample = {
            "macro_pass": scorers.macro_pass_rate(r, scn),
            "ban_violations": scorers.ban_violations(r, scn),
            "flagged": scorers.flagged_rate(r),
            "proteins": scorers.distinct_proteins(r),
            "repetition": scorers.repetition(r),
            "usd": usd,
            "revises": revises,
            "usd_per_dinner": usd / dinners,
            "latency_s": gen_ms / 1000,
            "_total_usd": total_usd,  # generation + judge; for the budget guard (not a scorecard metric)
        }
        if judge is not None:
            judged = judge.score(r)
            for axis in AXES:
                vals = [s[axis] for s in judged if s.get(axis) is not None]
                sample[axis] = sum(vals) / len(vals) if vals else 0.0
        out.append(sample)
    return out


def _mean(rows, key):
    return sum(row[key] for row in rows) / len(rows) if rows else 0.0


def _build_for(cand):
    """A build(config, conn) that runs the WHOLE pipeline on one candidate model, and seeds its
    price so v_llm_cost reports that model's $ (mirrors app.services wiring)."""
    def build(config, conn):
        conn.execute("INSERT OR REPLACE INTO model_prices (model, input_per_mtok, output_per_mtok, "
                     "search_per_ktok) VALUES (?, ?, ?, ?)",
                     (cand.model, cand.input_per_mtok, cand.output_per_mtok, cand.search_per_ktok))
        conn.commit()
        if cand.grounded:
            native = cand.base_url.rsplit("/openai", 1)[0]  # .../v1beta/openai -> .../v1beta
            agent = GeminiGroundedAgent(native, cand.model, cand.api_key)
        else:
            agent = OpenAICompatibleAgent(cand.base_url, cand.model, cand.api_key)
        picker = LLMCandidatePicker(cand.base_url, cand.model, cand.api_key)
        nutrition = LocalNutritionSource(connect(config.usda_db_path), conn, picker)
        return Services(agent, None, nutrition, None)
    return build


def _run_compare(config, scenarios, args, judge, metrics):
    cands = candidates()
    if not cands:
        print("No candidates configured — set LLM_*/ANTHROPIC_API_KEY or edit evals/models.py.")
        return
    budget = args.budget
    print(f"cost x quality A/B — {len(cands)} models over {len(scenarios)} scenarios "
          f"x {args.samples} sample(s)" + (f", judge={judge.model}" if judge else "")
          + (f", budget ${budget}" if budget else "") + "\n", flush=True)
    header = f"{'model':30}" + "".join(f"{_LABELS[m]:>12}" for m in metrics)
    print(header, flush=True)
    print("-" * len(header), flush=True)
    spent, stop = 0.0, False
    for cand in cands:
        if stop:
            break
        try:
            rows = []
            for scn in scenarios:
                r = run_scenario(config, scn, args.samples, build=_build_for(cand), judge=judge)
                rows.extend(r)
                spent += sum(x["_total_usd"] for x in r)
                if budget and spent >= budget:
                    stop = True
                    break
            if rows:
                means = {m: _mean(rows, m) for m in metrics}
                print(f"{cand.label:30}" + "".join(f"{means[m]:>12.3f}" for m in metrics), flush=True)
                _persist(config, cand.label, args.samples, {"aggregate": means})
        except Exception as e:  # noqa: BLE001 - one bad model shouldn't kill the sweep
            print(f"{cand.label:30}ERROR: {type(e).__name__}: {e}", flush=True)
    print(f"\ntotal spent: ~${spent:.2f}"
          + ("  (stopped early — budget reached)" if stop else ""), flush=True)


def _persist(config, model, samples, scorecard):
    results_db = config.db_path.parent / "evals.db"
    conn = connect(results_db)
    bootstrap(conn)
    run_id = conn.execute("INSERT INTO eval_runs (ts, model, samples) VALUES (?, ?, ?)",
                          (datetime.now(UTC).isoformat(), model, samples)).lastrowid
    conn.executemany(
        "INSERT INTO eval_scores (run_id, scenario, metric, value) VALUES (?, ?, ?, ?)",
        [(run_id, scn, m, v) for scn, means in scorecard.items() for m, v in means.items()],
    )
    conn.commit()
    return run_id, results_db


def _build_judge(config):
    """Judge from JUDGE_* env, falling back to the app LLM. Point JUDGE_* at a stronger /
    different-family model (e.g. Claude via your ANTHROPIC_API_KEY) for less self-preference bias."""
    base = os.environ.get("JUDGE_BASE_URL") or config.llm_base_url
    model = os.environ.get("JUDGE_MODEL") or config.llm_model
    key = os.environ.get("JUDGE_API_KEY") or config.llm_api_key or ""
    return LLMJudge(base, model, key), model


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--judge", action="store_true", help="also score appeal/realism/effort via an LLM judge")
    ap.add_argument("--compare", action="store_true", help="A/B every model in evals/models.py")
    ap.add_argument("--budget", type=float, default=None, help="stop the sweep once ~$ spent reaches this")
    args = ap.parse_args()
    config = load_config()
    scenarios = [s for s in SCENARIOS if args.scenario in (None, s.name)]
    if not scenarios:
        print(f"No scenario named {args.scenario!r}. Options: {[s.name for s in SCENARIOS]}")
        return

    judge = judge_model = None
    if args.judge:
        judge, judge_model = _build_judge(config)
    metrics = _BASE_METRICS + (list(AXES) if args.judge else [])

    if args.compare:
        _run_compare(config, scenarios, args, judge, metrics)
        return

    print(f"model={config.llm_model}  samples/scenario={args.samples}"
          + (f"  judge={judge_model}" if judge else "") + "\n")
    header = f"{'scenario':18}" + "".join(f"{_LABELS[m]:>12}" for m in metrics)
    print(header)
    print("-" * len(header))

    scorecard = {}
    for scn in scenarios:
        rows = run_scenario(config, scn, args.samples, judge=judge)
        means = {m: _mean(rows, m) for m in metrics}
        scorecard[scn.name] = means
        cells = "".join(f"{means[m]:>12.3f}" for m in metrics)
        print(f"{scn.name:18}{cells}")

    run_id, results_db = _persist(config, config.llm_model, args.samples, scorecard)
    print(f"\nSaved run #{run_id} to {results_db}")
    bans_broken = [s for s, m in scorecard.items() if m["ban_violations"] > 0]
    if bans_broken:
        print(f"⚠ BAN VIOLATIONS in: {', '.join(bans_broken)}  (must be 0)")


if __name__ == "__main__":
    main()
