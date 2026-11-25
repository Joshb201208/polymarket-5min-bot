"""The shipped owner watch list must parse completely."""

import json
from datetime import UTC, date, datetime

import pytest

from stock_agent import research_focus
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


_NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
_ACTIVE = {"added": "2026-11-25", "expires": "2027-01-25"}


def test_focus_cadence_and_first_three_active_directives(monkeypatch, tmp_path, caplog):
    path = tmp_path / "focus.json"
    rows = [
        {**_ACTIVE, "symbol": "OLD", "stance": "bullish", "owner_conviction": 8, "added": "2026-01-01",
         "expires": "2026-11-24"},
        {**_ACTIVE, "symbol": "A"},
        {**_ACTIVE, "symbol": "B", "cadence": "daily", "stance": "bullish", "owner_conviction": 7},
        {**_ACTIVE, "symbol": "C", "stance": "bullish", "owner_conviction": 8},
        {**_ACTIVE, "symbol": "C", "stance": "bullish", "owner_conviction": 9},
        {**_ACTIVE, "symbol": "D", "stance": "bullish", "owner_conviction": 9},
        {**_ACTIVE, "symbol": "E", "stance": "bullish", "owner_conviction": 8},
    ]
    path.write_text(json.dumps(rows))
    monkeypatch.setattr(research_focus, "load_focus", lambda *, now: load_focus(now=now, path=path))
    assert research_focus.focus_symbols(now=_NOW) == ("A", "B", "C", "D", "E")
    assert research_focus.focus_symbols(now=_NOW, cadence="live") == ("A", "C", "D", "E")
    assert research_focus.focus_symbols(now=_NOW, cadence="daily") == ("B",)
    directives = research_focus.owner_backed(now=_NOW)
    assert list(directives) == ["B", "C", "D"]
    assert [item.owner_conviction for item in directives.values()] == [7, 8, 9]
    assert "active limit is 3" in caplog.text


@pytest.mark.parametrize("item", [
    {"symbol": "A", "noise": 7}, {"symbol": "A", "cadence": "weekly"},
    {"symbol": "A", "stance": "bullish"}, {"symbol": "A", "owner_conviction": 8},
    {"symbol": "A", "stance": "bearish", "owner_conviction": 8},
    {"symbol": "A", "stance": "bullish", "owner_conviction": True},
    {"symbol": "A", "stance": "bullish", "owner_conviction": 8.0},
    {"symbol": "A", "stance": "bullish", "owner_conviction": 6},
    {"symbol": "A", "stance": "bullish", "owner_conviction": 10},
    {"theme": "Theme", "stance": "bullish", "owner_conviction": 8},
])
def test_invalid_focus_items_are_rejected(tmp_path, item):
    path = tmp_path / "focus.json"
    path.write_text(json.dumps([{**_ACTIVE, **item}]))
    assert not load_focus(now=_NOW, path=path)


def test_focus_file_retains_size_and_item_caps(tmp_path):
    path = tmp_path / "focus.json"
    path.write_text(json.dumps([{**_ACTIVE, "symbol": "A"}] * 26))
    assert not load_focus(now=_NOW, path=path)
    path.write_text(json.dumps([{**_ACTIVE, "symbol": "A"}]) + " " * 16_384)
    assert not load_focus(now=_NOW, path=path)
