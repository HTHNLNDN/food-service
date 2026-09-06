"""Review the ingredients the local food store couldn't resolve while planning.

These are the candidates to batch-add to the store (with real macros looked up) — start with
the ones that force revises most often. Run:  uv run python scripts/missing_ingredients.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.db import connect


def main() -> None:
    conn = connect(load_config().db_path)
    rows = conn.execute(
        "SELECT name, hits, caused_revise, sample_grams, last_seen FROM missing_ingredients "
        "ORDER BY caused_revise DESC, hits DESC"
    ).fetchall()
    if not rows:
        print("No unresolved ingredients logged yet.")
        return
    print(f"{'hits':>5} {'revises':>7} {'~grams':>7}  ingredient")
    print("-" * 48)
    for r in rows:
        print(f"{r['hits']:>5} {r['caused_revise']:>7} {r['sample_grams']:>7.0f}  {r['name']}")
    print(f"\n{len(rows)} distinct unresolved ingredients "
          f"({sum(r['caused_revise'] for r in rows)} revises total).")


if __name__ == "__main__":
    main()
