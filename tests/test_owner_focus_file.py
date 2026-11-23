"""The shipped owner watch list must parse completely."""

import json
from datetime import UTC, date, datetime

from stock_agent.research_focus import FOCUS_PATH, load_focus


def test_every_shipped_focus_entry_is_valid():
    rows = json.loads(FOCUS_PATH.read_text())
    assert isinstance(rows, list) and len(rows) <= 25
    for row in rows:
        # Loading on the entry's own start date proves the entry is accepted.
        start = date.fromisoformat(row["added"])
        now = datetime(start.year, start.month, start.day, 12, tzinfo=UTC)
        loaded = load_focus(now=now, path=FOCUS_PATH)
        keys = {item.symbol or item.theme for item in loaded}
        assert (row.get("symbol") or row.get("theme")) in keys
