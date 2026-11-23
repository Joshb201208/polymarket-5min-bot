"""Owner watch list. It affects priority, never eligibility to trade."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)
FOCUS_PATH = Path(__file__).with_name("owner_focus.json")
_SYMBOL = re.compile(r"[A-Z]{1,5}(?:[.-][A-Z])?")


@dataclass(frozen=True)
class FocusItem:
    symbol: str | None
    theme: str | None
    note: str
    added: date
    expires: date


def load_focus(*, now: datetime | None = None, path: Path = FOCUS_PATH) -> tuple[FocusItem, ...]:
    today = (now or datetime.now(UTC)).date()
    try:
        with path.open("rb") as stream:
            content = stream.read(16_385)
        if len(content) > 16_384:
            raise ValueError("focus list exceeds 16 KiB")
        rows = json.loads(content)
        if not isinstance(rows, list) or len(rows) > 25:
            raise ValueError("focus list must be a list of up to 25 items")
    except (OSError, ValueError):
        logger.warning("Cannot read owner focus list")
        return ()

    active: list[FocusItem] = []
    for index, item in enumerate(rows):
        try:
            if not isinstance(item, dict) or set(item) - {"symbol", "theme", "note", "added", "expires"}:
                raise ValueError
            symbol = item.get("symbol")
            theme = item.get("theme")
            if symbol is not None:
                symbol = symbol.strip().upper()
            if bool(symbol) == bool(theme):
                raise ValueError
            if symbol and not _SYMBOL.fullmatch(symbol):
                raise ValueError
            if theme and (not isinstance(theme, str) or not theme.strip() or len(theme) > 80):
                raise ValueError
            note = item.get("note", "")
            if not isinstance(note, str) or len(note) > 300:
                raise ValueError
            added = date.fromisoformat(item["added"])
            expires = date.fromisoformat(item["expires"])
            if expires < added:
                raise ValueError
            if added <= today <= expires:
                active.append(FocusItem(symbol, theme, note, added, expires))
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.warning("Invalid owner focus item at index %d", index)
    return tuple(active)


def focus_symbols(*, now: datetime | None = None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.symbol for item in load_focus(now=now) if item.symbol))
