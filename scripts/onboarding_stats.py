"""Onboarding sanity checks from the app database (nothing is sent anywhere).

    PYTHONPATH=. uv run python scripts/onboarding_stats.py

Started / completed / skipped / in progress / abandoned (started more than a week ago and
neither completed nor skipped), and the median minutes from start to completion.
"""

import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.db import connect

ABANDONED_AFTER = timedelta(days=7)


def stats(conn, now: datetime) -> dict:
    rows = conn.execute(
        "SELECT started_at, completed_at, skipped_at FROM user_onboarding").fetchall()
    open_rows = [r for r in rows if r["completed_at"] is None and r["skipped_at"] is None]
    minutes = [(r["completed_at"] - r["started_at"]).total_seconds() / 60
               for r in rows if r["completed_at"] is not None]
    abandoned = sum(1 for r in open_rows if now - r["started_at"] > ABANDONED_AFTER)
    return {
        "started": len(rows),
        "completed": len(minutes),
        "skipped": sum(1 for r in rows if r["skipped_at"] is not None),
        "in_progress": len(open_rows) - abandoned,
        "abandoned": abandoned,
        "median_minutes": statistics.median(minutes) if minutes else None,
    }


def main() -> None:
    with connect(load_config().database_url) as conn:
        result = stats(conn, datetime.now(UTC))
    for key, value in result.items():
        shown = "n/a" if value is None else (f"{value:.1f}" if isinstance(value, float) else value)
        print(f"{key:15} {shown}")
    if result["abandoned"]:
        print("\n⚠ some setups were started over a week ago and never finished or skipped")


if __name__ == "__main__":
    main()
