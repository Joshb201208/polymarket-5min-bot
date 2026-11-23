"""Print bounded, read-only ledger evidence without exporting raw SQLite state."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

QUERIES = {
    "open_position_counts": """
        SELECT asset_class, opened_source, COUNT(*) AS count
        FROM positions_meta WHERE closed_at IS NULL GROUP BY asset_class, opened_source
        ORDER BY count DESC LIMIT 50
    """,
    "intent_counts": """
        SELECT kind, status, COUNT(*) AS count FROM order_intents GROUP BY kind, status
        ORDER BY count DESC LIMIT 50
    """,
    "recent_jobs": """
        SELECT job, window_key, status, attempts, finished_at FROM job_runs
        ORDER BY COALESCE(finished_at, started_at) DESC LIMIT 40
    """,
    "recent_close_marks": """
        SELECT session_date, equity, cash, long_mv, short_mv, options_mv, taken_at
        FROM equity_marks WHERE quality = 'close' ORDER BY session_date DESC LIMIT 20
    """,
    "recent_reconciliations": """
        SELECT at, ok, n_diffs FROM reconcile_runs ORDER BY at DESC LIMIT 20
    """,
}


def summarize(db: Path) -> dict:
    if not db.is_file():
        return {"status": "state database not found"}
    try:
        # mode=ro still reads committed WAL pages; immutable=1 could omit them.
        with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            result = {"status": "ok"}
            for name, sql in QUERIES.items():
                try:
                    result[name] = [dict(row) for row in conn.execute(sql)]
                except sqlite3.Error as exc:
                    result[name] = {"status": "unavailable", "error": type(exc).__name__}
            return result
    except (OSError, sqlite3.Error) as exc:
        return {"status": "unavailable", "error": type(exc).__name__}


if __name__ == "__main__":
    print(json.dumps(summarize(Path(sys.argv[1])), indent=2, allow_nan=False))
