"""Owner watch list and explicitly bounded bullish research directives."""

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
_FIELDS = {"symbol", "theme", "note", "added", "expires", "cadence", "stance", "owner_conviction"}


@dataclass(frozen=True)
class FocusItem:
    symbol: str | None
    theme: str | None
    note: str
    added: date
    expires: date
    cadence: str = "live"
    stance: str | None = None
    owner_conviction: int | None = None


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
            if not isinstance(item, dict) or set(item) - _FIELDS:
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
            cadence = item.get("cadence", "live")
            stance = item.get("stance")
            owner_conviction = item.get("owner_conviction")
            if cadence not in ("live", "daily"):
                raise ValueError
            if stance is None:
                if owner_conviction is not None:
                    raise ValueError
            elif stance != "bullish" or not symbol or type(owner_conviction) is not int or not 7 <= owner_conviction <= 9:
                raise ValueError
            added = date.fromisoformat(item["added"])
            expires = date.fromisoformat(item["expires"])
            if expires < added:
                raise ValueError
            if added <= today <= expires:
                active.append(FocusItem(symbol, theme, note, added, expires, cadence, stance, owner_conviction))
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.warning("Invalid owner focus item at index %d", index)
    return tuple(active)


def focus_symbols(*, now: datetime | None = None, cadence: str | None = None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        item.symbol for item in load_focus(now=now) if item.symbol and (cadence is None or item.cadence == cadence)
    ))


def owner_backed(*, now: datetime | None = None) -> dict[str, FocusItem]:
    """First three distinct, active directives in file order."""
    items: dict[str, FocusItem] = {}
    for item in load_focus(now=now):
        if not item.symbol or item.stance is None:
            continue
        if item.symbol in items:
            logger.warning("Ignoring duplicate owner-backed directive for %s", item.symbol)
        elif len(items) >= 3:
            logger.warning("Ignoring owner-backed directive for %s: active limit is 3", item.symbol)
        else:
            items[item.symbol] = item
    return items
