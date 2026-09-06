"""Summarize LLM spend from the llm_calls log.

Costs come from the editable model_prices table via the v_llm_cost view. Run:
    uv run python scripts/llm_cost.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.db import connect


def main() -> None:
    conn = connect(load_config().db_path)
    rows = conn.execute(
        "SELECT call_type, model, COUNT(*) n, COALESCE(SUM(input_tokens),0) in_tok, "
        "COALESCE(SUM(output_tokens),0) out_tok, COALESCE(SUM(search_queries),0) sq, "
        "SUM(usd) usd, COALESCE(SUM(retries),0) retries, "
        "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) errs, AVG(latency_ms) ms "
        "FROM v_llm_cost GROUP BY call_type, model ORDER BY usd DESC"
    ).fetchall()
    if not rows:
        print("No LLM calls logged yet. Generate a plan, then re-run.")
        return

    print(f"{'call_type':15} {'model':17} {'n':>4} {'in tok':>9} {'out tok':>9} {'srch':>5} "
          f"{'$':>8} {'ms/call':>7} {'retry':>5} {'err':>4}")
    print("-" * 92)
    for r in rows:
        print(f"{r['call_type']:15} {r['model']:17.17} {r['n']:>4} {r['in_tok']:>9} "
              f"{r['out_tok']:>9} {r['sq']:>5} {r['usd']:>8.3f} {(r['ms'] or 0):>7.0f} "
              f"{r['retries']:>5} {r['errs']:>4}")

    tot = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(usd),0) usd FROM v_llm_cost").fetchone()
    nplans = conn.execute(
        "SELECT COUNT(DISTINCT plan_id) n FROM llm_calls WHERE plan_id IS NOT NULL"
    ).fetchone()["n"]
    per_plan = f"  (~${tot['usd'] / nplans:.3f}/plan across {nplans} plans)" if nplans else ""
    print(f"\nTotal: {tot['n']} calls, ${tot['usd']:.2f}{per_plan}")
    unpriced = [r["model"] for r in conn.execute(
        "SELECT DISTINCT model FROM llm_calls WHERE model NOT IN (SELECT model FROM model_prices)"
    )]
    if unpriced:
        print(f"No price set for: {', '.join(unpriced)} — add rows to model_prices.")


if __name__ == "__main__":
    main()
