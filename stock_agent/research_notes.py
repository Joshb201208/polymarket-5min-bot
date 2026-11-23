"""Optional, bounded context from explicitly approved research-note files."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterable

from stock_agent.config import Config

logger = logging.getLogger(__name__)
_SYMBOL = re.compile(r"[A-Z]{1,5}(?:[.-][A-Z])?")
_MAX_NOTE_BYTES = 8_192


@dataclass(frozen=True)
class ResearchNote:
    path: Path
    source: str
    note_date: date
    expires: date
    symbols: tuple[str, ...]
    body: str

    def applies_to(self, symbol: str) -> bool:
        return "ALL" in self.symbols or symbol.upper() in self.symbols


def load_notes(
    notes_dir: Path | str | None = None, *, enabled: bool = False, today: date | None = None,
) -> list[ResearchNote]:
    if not enabled:
        return []
    root = Path(notes_dir) if notes_dir is not None else Path(Config.DATA_DIR) / "research_notes"
    day = today or datetime.now(UTC).date()
    notes = []
    for path in sorted(root.glob("*"))[:30]:
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {".txt", ".md"}:
            continue
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_NOTE_BYTES + 1)
            if len(raw) > _MAX_NOTE_BYTES:
                raise ValueError
            lines = raw.decode("utf-8").splitlines()
            if not lines or lines[0] != "---":
                raise ValueError
            stop = lines.index("---", 1)
            meta = {}
            for pair in lines[1:stop]:
                key, value = map(str.strip, pair.split(":", 1))
                if key in meta:
                    raise ValueError
                meta[key] = value
            if set(meta) != {"source", "date", "expires", "symbols", "provider_share"}:
                raise ValueError
            symbols = tuple(s.strip().upper() for s in meta["symbols"].split(","))
            if meta["provider_share"] != "approved" or not all(s == "ALL" or _SYMBOL.fullmatch(s) for s in symbols):
                raise ValueError
            note_date, expires = date.fromisoformat(meta["date"]), date.fromisoformat(meta["expires"])
            body = "\n".join(lines[stop + 1:]).strip()
            if expires < note_date or not body or not 0 < len(meta["source"]) <= 100:
                raise ValueError
            if note_date <= day <= expires:
                notes.append(ResearchNote(path, meta["source"], note_date, expires, symbols, body))
        except (UnicodeError, ValueError, OSError):
            logger.warning("Skipped malformed or unapproved research note")
    return notes


def research_notes_payload(symbol: str, notes: Iterable[ResearchNote]) -> list[dict]:
    selected = [n for n in notes if n.applies_to(symbol)]
    return [{"source": n.source, "date": n.note_date.isoformat(), "body": n.body[:700]} for n in selected[:3]]


def format_research_notes_context(symbol: str, notes: Iterable[ResearchNote]) -> str:
    approved = research_notes_payload(symbol, notes)
    return json.dumps(approved, ensure_ascii=False) if approved else ""
