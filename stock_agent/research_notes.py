"""Research notes loader.

Loads strategist/research notes from ``data/stock_agent/research_notes/`` and
formats them as supplementary context for the analyst. This lets us feed
external strategist views (e.g. Tom Lee / Fundstrat sector tilts) into the
bot's per-symbol thesis without changing how the bot makes its own decision.

Notes are plain Markdown or text files. Each note may include front-matter:

    ---
    source: Fundstrat - Tom Lee FIRST WORD
    date: 2026-05-01
    expires: 2026-06-01   # optional; notes past this date are skipped
    symbols: MSFT, GOOGL, AMZN, CRM, ADBE, IGV, AMD, ANET, AVGO, BK, GS
    ---
    Body of the note...

If ``symbols`` is provided, the note is only injected when the analyzed
symbol is in that list (or always, if the symbol list is empty / contains
``ALL``). Otherwise, the note is treated as macro/strategy guidance and
included for every symbol.

The bot remains the decision-maker — these notes are framed as
"context to consider", never as instructions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from stock_agent.config import Config

logger = logging.getLogger(__name__)


@dataclass
class ResearchNote:
    path: Path
    source: str
    note_date: date | None
    expires: date | None
    symbols: list[str]  # uppercase; empty or ["ALL"] means apply to every symbol
    body: str

    def applies_to(self, symbol: str) -> bool:
        if not self.symbols or "ALL" in self.symbols:
            return True
        return symbol.upper() in self.symbols

    def is_active(self, today: date | None = None) -> bool:
        today = today or date.today()
        if self.expires and today > self.expires:
            return False
        return True


_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """Extract optional YAML-ish front matter. Returns (meta_dict, body)."""
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end():]
    meta: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip()
    return meta, body


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _parse_symbols(s: str | None) -> list[str]:
    if not s:
        return []
    parts = [p.strip().upper() for p in re.split(r"[,\s]+", s) if p.strip()]
    return parts


def load_notes(notes_dir: Path | str | None = None) -> list[ResearchNote]:
    """Load all valid research notes from disk."""
    if notes_dir is None:
        notes_dir = Path(Config.DATA_DIR) / "research_notes"
    notes_dir = Path(notes_dir)
    if not notes_dir.exists():
        return []

    notes: list[ResearchNote] = []
    for p in sorted(notes_dir.glob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".md", ".txt"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to read research note %s: %s", p, e)
            continue
        meta, body = _parse_front_matter(text)
        note = ResearchNote(
            path=p,
            source=meta.get("source", p.stem),
            note_date=_parse_date(meta.get("date")),
            expires=_parse_date(meta.get("expires")),
            symbols=_parse_symbols(meta.get("symbols")),
            body=body.strip(),
        )
        if note.is_active():
            notes.append(note)
    return notes


def format_research_notes_context(symbol: str, notes: Iterable[ResearchNote]) -> str:
    """Format applicable research notes for the given symbol as supplementary context.

    Returns "" if no notes apply, so the analyst prompt stays clean.
    """
    applicable = [n for n in notes if n.applies_to(symbol)]
    if not applicable:
        return ""

    lines = ["EXTERNAL RESEARCH NOTES (context to consider — you remain the decision-maker):"]
    for n in applicable:
        date_str = n.note_date.strftime("%Y-%m-%d") if n.note_date else "undated"
        lines.append(f"\n--- {n.source} ({date_str}) ---")
        lines.append(n.body)
    lines.append(
        "\nUse these as one input among many. If the note disagrees with the "
        "fundamentals or your own analysis, weight the fundamentals more heavily. "
        "Do not blindly defer."
    )
    return "\n".join(lines)
