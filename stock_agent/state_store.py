"""Versioned SQLite state store (stdlib ``sqlite3``).

One file, ``DATA_DIR/state.db``, holds everything the bot must remember that
the broker cannot tell it: position metadata (thesis link, entry date, sector,
stop), theses, order intents, job markers, equity close marks, cash flows,
risk state, reconcile reports and an archive of the legacy JSON state.

Rules:

* The store never silently starts fresh. If the file cannot be opened, fails
  ``PRAGMA integrity_check`` or has an unknown schema version, it is renamed
  ``state.db.corrupt-<ts>`` (never deleted), a new file is created, and
  ``trading_block()`` returns a reason until a clean broker reconcile calls
  ``clear_trading_block()``.
* Schema changes are numbered migrations. Other areas add their own tables
  by registering a migration with an ``owner`` name (see
  ``register_migration``); migrations are applied in (version, owner) order.
* ``transaction()`` is ``BEGIN IMMEDIATE`` so multi-table writes are atomic.
* ``backup()`` uses the sqlite backup API into ``backups/state-YYYYMMDD.db``
  and keeps ``keep`` copies.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1  # core schema version owned by state_ledger


class StateUnavailable(Exception):
    """The state store cannot be used. Trading must stop."""


@dataclass
class StoreHealth:
    ok: bool
    created: bool = False            # a new empty database was created
    quarantined: Path | None = None  # path the unreadable file was moved to
    migrated_v0: bool = False        # legacy JSON files were migrated on this open
    trading_block: str | None = None
    notes: list[str] = field(default_factory=list)


Migration = Callable[[sqlite3.Connection], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_CORE_V1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions_meta (
    position_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL DEFAULT 'us_equity',
    opened_at TEXT,
    opened_source TEXT NOT NULL DEFAULT 'bot',
    closed_at TEXT,
    close_cause TEXT,
    sector TEXT,
    stop_price REAL,
    stop_order_id TEXT,
    stop_expires_at TEXT,
    entry_thesis_id TEXT,
    entry_intent_id TEXT,
    notes_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_pm_symbol_open ON positions_meta(symbol, closed_at);
CREATE TABLE IF NOT EXISTS theses (
    thesis_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    generated_at TEXT,
    source TEXT NOT NULL DEFAULT '',
    decision_id TEXT,
    supersedes TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_theses_symbol ON theses(symbol, created_at);
CREATE TABLE IF NOT EXISTS current_thesis (
    symbol TEXT PRIMARY KEY, thesis_id TEXT NOT NULL, set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_activities (
    activity_id TEXT PRIMARY KEY,
    activity_type TEXT NOT NULL,
    ts TEXT NOT NULL,
    symbol TEXT,
    qty REAL,
    price REAL,
    side TEXT,
    order_id TEXT,
    net_amount REAL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_act_ts ON broker_activities(ts);
CREATE INDEX IF NOT EXISTS ix_act_symbol ON broker_activities(symbol, ts);
CREATE TABLE IF NOT EXISTS order_intents (
    intent_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    dedupe_key TEXT NOT NULL UNIQUE,
    decision_id TEXT,
    job TEXT,
    window_key TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'equity',
    position_id TEXT,
    thesis_ids_json TEXT NOT NULL DEFAULT '[]',
    reference_price REAL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    origin TEXT,
    sleeve TEXT,
    created_at TEXT NOT NULL,
    session_date TEXT,
    expires_at TEXT,
    next_attempt_at TEXT,
    status TEXT NOT NULL,
    broker_order_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_intents_status ON order_intents(status);
CREATE TABLE IF NOT EXISTS job_runs (
    job TEXT NOT NULL,
    window_key TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    eligible_retry_at TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (job, window_key)
);
CREATE TABLE IF NOT EXISTS equity_marks (
    session_date TEXT NOT NULL,
    quality TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL,
    long_mv REAL,
    short_mv REAL,
    options_mv REAL,
    taken_at TEXT NOT NULL,
    extra_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_date, quality)
);
CREATE TABLE IF NOT EXISTS cash_flows (
    activity_id TEXT PRIMARY KEY, date TEXT NOT NULL, amount REAL NOT NULL, kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reconcile_runs (
    run_id TEXT PRIMARY KEY, at TEXT NOT NULL, reason TEXT NOT NULL, ok INTEGER NOT NULL,
    n_diffs INTEGER NOT NULL, report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_archive (name TEXT PRIMARY KEY, archived_at TEXT NOT NULL, raw_json TEXT NOT NULL);
"""


def _core_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(_CORE_V1)


class StateStore:
    """The single SQLite state file. Thread-safe for one process."""

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, path: Path, journal: Any | None = None, *, backup_keep: int = 14,
                 clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.journal = journal
        self.backup_keep = backup_keep
        self._clock = clock or _utcnow
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._migrations: dict[tuple[str, int], Migration] = {("core", 1): _core_v1}
        self._block_reason: str | None = None

    # ------------------------------------------------------------ migrations
    def register_migration(self, version: int, owner: str, fn: Migration) -> None:
        """Register a table migration owned by another area (e.g. 'research', 'options').

        Must be called before ``open()`` or followed by ``apply_migrations()``.
        Each (owner, version) runs once; record kept in ``meta``.
        """
        self._migrations[(owner, int(version))] = fn

    def apply_migrations(self) -> list[str]:
        applied: list[str] = []
        conn = self.conn
        for (owner, version) in sorted(self._migrations, key=lambda k: (k[0] != "core", k[0], k[1])):
            key = f"schema:{owner}"
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone() if self._has_meta() else None
            have = int(row[0]) if row else 0
            if version <= have:
                continue
            with self.transaction() as c:
                self._migrations[(owner, version)](c)
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(version)))
            applied.append(f"{owner}:{version}")
        return applied

    def _has_meta(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() is not None

    # ------------------------------------------------------------------ open
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StateUnavailable("state store is not open")
        return self._conn

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def open(self) -> StoreHealth:
        """Open (creating or migrating as needed). Never silently fresh.

        Returns a ``StoreHealth``. Raises ``StateUnavailable`` only when even a
        new file cannot be created (for example the disk is full or read-only).
        """
        health = StoreHealth(ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists() and self.path.stat().st_size > 0
        if existed:
            problem = self._check_existing()
            if problem:
                q = self._quarantine(problem)
                health.quarantined = q
                health.notes.append(f"state.db unreadable ({problem}); moved to {q.name}")
                self._block_reason = f"state unreadable ({problem}); rebuild from broker required"
                existed = False
        try:
            self._conn = self._connect()
            self.apply_migrations()
        except sqlite3.Error as e:
            self._conn = None
            raise StateUnavailable(f"cannot create state store: {e}") from e
        if not existed:
            health.created = True
            self.set_meta("created_at", self._clock().isoformat())
        persisted = self.get_meta("trading_block")
        if persisted and not self._block_reason:
            self._block_reason = persisted
        if self._block_reason:
            self.set_meta("trading_block", self._block_reason)
        health.trading_block = self._block_reason
        health.ok = self._block_reason is None
        self._journal("state_event", event="open", created=health.created,
                      quarantined=str(health.quarantined) if health.quarantined else None,
                      trading_block=self._block_reason)
        return health

    def _check_existing(self) -> str | None:
        try:
            conn = sqlite3.connect(str(self.path), timeout=10)
            try:
                res = conn.execute("PRAGMA integrity_check").fetchone()
                if not res or res[0] != "ok":
                    return f"integrity_check: {res[0] if res else 'no result'}"
                has_meta = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
                if not has_meta:
                    return "no meta table"
                row = conn.execute("SELECT value FROM meta WHERE key='schema:core'").fetchone()
                if not row:
                    return "no schema version"
                if int(row[0]) > SCHEMA_VERSION:
                    return f"schema version {row[0]} is newer than this code ({SCHEMA_VERSION})"
            finally:
                conn.close()
        except (sqlite3.Error, ValueError) as e:
            return f"{type(e).__name__}: {e}"
        return None

    def _quarantine(self, reason: str) -> Path:
        ts = self._clock().strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"{self.path.name}.corrupt-{ts}")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.path) + suffix)
            if src.exists():
                src.replace(Path(str(target) + suffix))
        logger.critical("state store quarantined: %s -> %s (%s)", self.path, target, reason)
        return target

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ----------------------------------------------------------- transactions
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic multi-statement write (``BEGIN IMMEDIATE``). Re-entrant."""
        with self._lock:
            conn = self.conn
            if conn.in_transaction:
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # --------------------------------------------------------- meta / kv
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self.transaction() as c:
            if value is None:
                c.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value_json FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self.transaction() as c:
            c.execute("INSERT OR REPLACE INTO kv(key, value_json, updated_at) VALUES (?, ?, ?)",
                      (key, json.dumps(value, default=str), self._clock().isoformat()))

    # ----------------------------------------------------------- trading block
    def trading_block(self) -> str | None:
        """Reason trading must not place new orders, or None."""
        return self._block_reason

    def set_trading_block(self, reason: str) -> None:
        self._block_reason = reason
        self.set_meta("trading_block", reason)
        self._journal("state_event", event="trading_block", reason=reason)

    def clear_trading_block(self, by: str) -> None:
        if self._block_reason is None:
            return
        old = self._block_reason
        self._block_reason = None
        self.set_meta("trading_block", None)
        self._journal("state_event", event="trading_unblocked", previous=old, by=by)

    # ---------------------------------------------------------------- backup
    def backup(self, when: datetime | None = None) -> Path:
        """Consistent copy to ``backups/state-YYYYMMDD.db``; keeps ``backup_keep`` newest."""
        when = when or self._clock()
        bdir = self.path.parent / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        target = bdir / f"state-{when.strftime('%Y%m%d')}.db"
        tmp = target.with_name(target.name + ".tmp")
        with self._lock:
            dst = sqlite3.connect(str(tmp))
            try:
                self.conn.backup(dst)
            finally:
                dst.close()
        tmp.replace(target)
        backups = sorted(bdir.glob("state-*.db"))
        for old in backups[: max(0, len(backups) - self.backup_keep)]:
            try:
                old.unlink()
            except OSError:
                pass
        self._journal("state_event", event="backup", path=target.name)
        return target

    # ----------------------------------------------------------------- misc
    def _journal(self, kind: str, **fields: Any) -> None:
        if self.journal is not None:
            self.journal.append(kind, **fields)

    def now(self) -> datetime:
        return self._clock()
