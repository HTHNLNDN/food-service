"""Pre-merge eval gate: run the deterministic eval on the CURRENT model (app config) over a few
high-signal scenarios and exit non-zero if a quality/cost threshold is breached. Run it before
shipping a prompt or model change. No judge (cheap, deterministic).

    uv run python evals/gate.py            # core scenarios, 1 sample (~$0.03)
    uv run python evals/gate.py --full --samples 3

Thresholds are informed by the full sweep (Gemini 3.6 Flash grounded: macro-pass ~0.91, 0 ban
violations, ~0.03 flagged, $0.001/dinner). It runs real LLM calls, so it's a local/manual gate,
not something to wire into keyless CI.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from evals.run import _mean, run_scenario
from evals.scenarios import SCENARIOS

_CORE = ("baseline", "tight_macros", "no_fish", "vegan")

# (metric, op, threshold) — a change must not breach these.
GATES = [
    ("ban_violations", "<=", 0.0),   # hard: a banned ingredient must never survive
    ("macro_pass", ">=", 0.80),      # most dinners on-target
    ("flagged", "<=", 0.20),         # few off-target/unresolvable dishes
    ("usd_per_dinner", "<=", 0.010),  # ~10x headroom over the $0.001 baseline
]


def check_gates(means: dict, gates=GATES) -> list[tuple]:
    """Return the breached (metric, op, threshold, value) tuples — empty means the gate passes."""
    failures = []
    for metric, op, thr in gates:
        v = means.get(metric, 0.0)
        ok = v <= thr if op == "<=" else v >= thr
        if not ok:
            failures.append((metric, op, thr, v))
    return failures


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="all scenarios (default: core subset)")
    ap.add_argument("--samples", type=int, default=1)
    args = ap.parse_args()
    config = load_config()
    scns = list(SCENARIOS) if args.full else [s for s in SCENARIOS if s.name in _CORE]

    print(f"gate: model={config.llm_model}, {len(scns)} scenarios x {args.samples} sample(s)\n")
    rows = []
    for scn in scns:
        rows.extend(run_scenario(config, scn, args.samples))
    means = {m: _mean(rows, m) for m in
             ("macro_pass", "ban_violations", "flagged", "proteins", "repetition", "usd_per_dinner")}
    for m, v in means.items():
        print(f"  {m:16} {v:.3f}")

    failures = check_gates(means)
    print()
    for metric, op, thr, v in failures:
        print(f"  FAIL  {metric} = {v:.3f}, needs {op} {thr}")
    if failures:
        print(f"\n✗ gate FAILED — {len(failures)} threshold(s) breached")
        sys.exit(1)
    print("✓ gate PASSED")


if __name__ == "__main__":
    main()
