"""One-time import of the legacy JSON state (v0) into the SQLite store.

The broker is the truth for quantities, cash and equity; the legacy files only
contribute what the broker cannot know: sector, stop, entry thesis, research
theses, the old curve and trade log (archived, never headline), and when the
last weekly analysis ran.

Rules
-----
* One transaction: either everything is imported or nothing is.
* Idempotent: a store that already records ``migrated_v0_at`` is left alone.
* Never deletes or edits the legacy files.  They are copied to
  ``<data_dir>/legacy_v0/`` and left in place, because ``Portfolio`` (the shim
  that older callers still use) keeps reading ``portfolio.json`` until every
  area has moved to the ledger.
* Entry dates are provisional (``opened_source='v0'``): the next reconcile
  replaces them with the date of the first broker fill after flat, which fixes
  placeholders such as positions imported with the date they were noticed.
* Placeholder theses ("Imported from Alpaca ... awaiting re-analysis") are kept
  as ``LEGACY_UNVERIFIED`` and never become an entry thesis.
* ``dry_run=True`` does all the work inside the transaction, then rolls back.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ledger import ThesisRole, new_id
from .state_store import StateStore

LEGACY_FILES = ("portfolio.json", "options_portfolio.json", "macro_state.json")
PLACEHOLDER_MARKERS = ("awaiting re-analysis", "imported from alpaca")

# Optional hook owned by the options area: (conn, open_option_rows) -> number imported.
OptionsImporter = Callable[[Any, list[dict]], int]


class _DryRun(Exception):
    pass


@dataclass
class MigrationReport:
    ran: bool = False
    dry_run: bool = False
    skipped_reason: str | None = None
    positions: int = 0
    entry_theses: int = 0
    legacy_unverified: int = 0
    held_review: int = 0
    entry_candidates: int = 0
    equity_marks: int = 0
    trades_archived: int = 0
    options_open: int = 0
    options_open_imported: int = 0
    options_closed_archived: int = 0
    jobs: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ran:
            return f"v0 migration skipped: {self.skipped_reason}"
        mode = "dry run" if self.dry_run else "applied"
        return (f"v0 migration {mode}: {self.positions} positions, {self.entry_theses} entry theses, "
                f"{self.legacy_unverified} legacy-unverified, {self.held_review} held reviews, "
                f"{self.entry_candidates} entry candidates, {self.equity_marks} legacy equity marks, "
                f"{self.trades_archived} trades archived, {self.options_open} open option groups "
                f"({self.options_open_imported} imported), {self.options_closed_archived} closed option "
                f"groups archived, jobs={self.jobs}, warnings={len(self.warnings)}")


def has_legacy_state(data_dir: Path) -> bool:
    return (Path(data_dir) / "portfolio.json").exists()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_ts(s: Any) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_summary_date(s: Any) -> str | None:
    """Legacy summaries use 'Mar 30, 2026 (Mon)'; accept ISO too."""
    if not isinstance(s, str) or not s:
        return None
    head = s.split(" (")[0].strip()
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(head, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def is_placeholder_thesis(t: Any) -> bool:
    if not isinstance(t, dict):
        return True
    text = " ".join(str(t.get(k) or "") for k in ("summary", "bull_case")).lower()
    return any(m in text for m in PLACEHOLDER_MARKERS)


def _load(path: Path, report: MigrationReport) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        report.warnings.append(f"{path.name} unreadable ({type(e).__name__}); not imported")
        return None


def _num(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def migrate_v0(data_dir: Path, store: StateStore, *, dry_run: bool = False,
               options_importer: OptionsImporter | None = None,
               now: datetime | None = None) -> MigrationReport:
    data_dir = Path(data_dir)
    report = MigrationReport(dry_run=dry_run)
    if store.get_meta("migrated_v0_at"):
        report.skipped_reason = "already migrated"
        return report
    pf = _load(data_dir / "portfolio.json", report)
    if not isinstance(pf, dict):
        report.skipped_reason = "no readable portfolio.json"
        return report
    opts = _load(data_dir / "options_portfolio.json", report)
    macro = _load(data_dir / "macro_state.json", report)
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()

    def thesis(c, symbol: str, payload: dict, role: ThesisRole, current: bool) -> str:
        tid = new_id("th")
        gen = _parse_ts(payload.get("generated_at")) if isinstance(payload, dict) else None
        body = {**(payload if isinstance(payload, dict) else {"raw": payload}), "legacy_v0": True}
        c.execute("INSERT INTO theses(thesis_id, symbol, role, created_at, generated_at, source, decision_id,"
                  " supersedes, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
                  (tid, symbol, role.value, stamp, _iso(gen), "v0", None, None, json.dumps(body, default=str)))
        if current:
            c.execute("INSERT OR REPLACE INTO current_thesis(symbol, thesis_id, set_at) VALUES (?,?,?)",
                      (symbol, tid, stamp))
        return tid

    def archive(c, name: str, value: Any) -> None:
        if value is None:
            return
        c.execute("INSERT OR REPLACE INTO legacy_archive(name, archived_at, raw_json) VALUES (?,?,?)",
                  (name, stamp, json.dumps(value, default=str)))
        report.archived.append(name)

    try:
        with store.transaction() as c:
            held: set[str] = set()
            for p in pf.get("positions") or []:
                sym = str(p.get("symbol") or "").upper()
                if not sym:
                    report.warnings.append("position without symbol skipped")
                    continue
                held.add(sym)
                t = p.get("thesis")
                placeholder = is_placeholder_thesis(t)
                entry_tid = None
                if t is not None:
                    role = ThesisRole.LEGACY_UNVERIFIED if placeholder else ThesisRole.ENTRY_CANDIDATE
                    tid = thesis(c, sym, t, role, current=False)
                    if placeholder:
                        report.legacy_unverified += 1
                    else:
                        entry_tid = tid
                        report.entry_theses += 1
                opened = None if placeholder else _parse_ts(p.get("entry_date"))
                notes = {"v0": {"shares": p.get("shares"), "entry_price": p.get("entry_price"),
                                "entry_date": p.get("entry_date"), "placeholder": placeholder}}
                c.execute("INSERT INTO positions_meta(position_id, symbol, asset_class, opened_at, opened_source,"
                          " sector, stop_price, entry_thesis_id, notes_json) VALUES (?,?,?,?,?,?,?,?,?)",
                          (new_id("pos"), sym, "us_equity", _iso(opened), "v0", p.get("sector") or None,
                           _num(p.get("stop_loss")) or None, entry_tid, json.dumps(notes, default=str)))
                report.positions += 1

            for key, t in (pf.get("active_theses") or {}).items():
                sym = str((t or {}).get("symbol") or key).upper()
                if is_placeholder_thesis(t):
                    thesis(c, sym, t, ThesisRole.LEGACY_UNVERIFIED, current=False)
                    report.legacy_unverified += 1
                elif sym in held:
                    thesis(c, sym, t, ThesisRole.HELD_REVIEW, current=True)
                    report.held_review += 1
                else:
                    thesis(c, sym, t, ThesisRole.ENTRY_CANDIDATE, current=True)
                    report.entry_candidates += 1

            for s in pf.get("daily_summaries") or []:
                d = _parse_summary_date(s.get("date"))
                eq = _num(s.get("portfolio_value"))
                if d is None or eq is None:
                    continue
                extra = {k: s.get(k) for k in ("day_pnl", "total_pnl", "num_positions", "exposure_pct")}
                cur = c.execute("INSERT OR IGNORE INTO equity_marks(session_date, quality, equity, cash, taken_at,"
                                " extra_json) VALUES (?,?,?,?,?,?)",
                                (d, "legacy", eq, _num(s.get("cash")), stamp, json.dumps(extra, default=str)))
                report.equity_marks += cur.rowcount

            trades = pf.get("trade_history") or []
            report.trades_archived = len(trades)
            archive(c, "v0.trade_history", trades)
            archive(c, "v0.daily_summaries", pf.get("daily_summaries") or [])
            archive(c, "v0.portfolio_scalars",
                    {k: pf.get(k) for k in ("cash", "starting_capital", "last_universe_refresh", "universe",
                                            "last_weekly_analysis", "last_daily_check")})

            lw = _parse_ts(pf.get("last_weekly_analysis"))
            if lw:
                y, w, _ = lw.isocalendar()
                wk = f"{y}-W{w:02d}"
                c.execute("INSERT OR IGNORE INTO job_runs(job, window_key, status, attempts, started_at, finished_at,"
                          " detail_json) VALUES (?,?,?,?,?,?,?)",
                          ("weekly", wk, "done", 1, _iso(lw), _iso(lw), json.dumps({"source": "v0"})))
                report.jobs.append(f"weekly:{wk}")
            for k in ("last_daily_check", "last_universe_refresh"):
                if pf.get(k):
                    c.execute("INSERT OR REPLACE INTO kv(key, value_json, updated_at) VALUES (?,?,?)",
                              (f"v0.{k}", json.dumps(pf.get(k)), stamp))

            if isinstance(opts, dict):
                open_rows = [o for o in opts.get("positions") or [] if o.get("is_open", True)]
                closed = list(opts.get("closed_positions") or []) + \
                    [o for o in opts.get("positions") or [] if not o.get("is_open", True)]
                report.options_open = len(open_rows)
                report.options_closed_archived = len(closed)
                archive(c, "v0.options_open", open_rows)
                archive(c, "v0.options_closed", closed)
                archive(c, "v0.options_scalars", {k: v for k, v in opts.items()
                                                  if k not in ("positions", "closed_positions")})
                if open_rows and options_importer is not None:
                    report.options_open_imported = int(options_importer(c, open_rows) or 0)
                elif open_rows:
                    report.warnings.append("open option groups archived only (no options importer registered);"
                                           " reconcile will flag their legs as ungrouped")
            if isinstance(macro, dict):
                archive(c, "v0.macro_state", macro)

            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", ("migrated_v0_at", stamp))
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                      ("migrated_v0_report", report.summary().replace("v0 migration applied", "v0 migration")))
            report.ran = True
            if dry_run:
                raise _DryRun
    except _DryRun:
        return report

    _copy_originals(data_dir, report)
    if store.journal is not None:
        store.journal.append("state_event", event="migrated_v0", summary=report.summary(),
                             warnings=report.warnings)
    return report


def _copy_originals(data_dir: Path, report: MigrationReport) -> None:
    dest = data_dir / "legacy_v0"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for name in LEGACY_FILES:
            src = data_dir / name
            if src.exists() and not (dest / name).exists():
                shutil.copy2(src, dest / name)
    except OSError as e:
        report.warnings.append(f"could not copy legacy files ({type(e).__name__}); originals untouched")
