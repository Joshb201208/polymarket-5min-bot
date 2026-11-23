"""Permanent append-only journal (JSON lines).

Every decision, thesis, order intent, order, fill, reconcile difference,
shadow-stop record and news reaction is appended here as one JSON object per
line. Nothing is ever deleted: the file rotates monthly and closed months are
gzip-compressed in place (``journal-YYYY-MM.jsonl.gz``).

The journal must never break trading: ``append`` swallows its own errors,
logs them, and reports them through ``health()`` so the caller can alert.

Record shape::

    {"seq": 17, "ts": "2026-11-23T15:04:05.123456+00:00", "kind": "fill", "v": 1, ...fields}

``kind`` is a free string; well-known kinds are listed in ``KINDS``.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

SCHEMA_V = 1

# Well-known kinds. Others are allowed; keep names short and lower_snake.
KINDS = (
    "decision",        # a trading decision (buy/sell/hold/refused) with reasons
    "thesis",          # a thesis was recorded (payload included)
    "intent",          # an order intent was queued / changed status
    "order",           # an order was sent / acknowledged by the broker
    "fill",            # a broker fill was ingested
    "reconcile_diff",  # one difference found by reconcile
    "reconcile",       # summary of one reconcile run
    "shadow_stop",     # what alternative stop rules would have done (A3)
    "news_reaction",   # a live-news reaction decision (A5)
    "alert",           # an alert that was raised
    "state_event",     # store opened / quarantined / rebuilt / backup
    "migration",       # v0 -> v1 migration report
)

# fsync after these kinds even when the journal is not in "sync every line" mode.
_DURABLE_KINDS = frozenset({"order", "fill", "intent", "reconcile_diff", "state_event", "migration"})


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        return o.isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if hasattr(o, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(o)
    if isinstance(o, Path):
        return str(o)
    return repr(o)


@dataclass
class JournalHealth:
    ok: bool = True
    last_error: str | None = None
    errors: int = 0
    bytes_used: int = 0
    max_bytes: int = 0
    near_cap: bool = False
    notes: list[str] = field(default_factory=list)


class Journal:
    """Append-only JSONL journal with monthly rotation and gzip of old months."""

    def __init__(
        self,
        directory: Path,
        *,
        max_mb: float = 500.0,
        clock: Callable[[], datetime] | None = None,
        fsync_all: bool = False,
    ):
        self.directory = Path(directory)
        self.max_bytes = int(max_mb * 1024 * 1024)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._fsync_all = fsync_all
        self._lock = threading.Lock()
        self._seq: int | None = None
        self._health = JournalHealth(max_bytes=self.max_bytes)
        self._compressed_upto: str | None = None

    # ------------------------------------------------------------------ paths
    def path_for(self, when: datetime) -> Path:
        return self.directory / f"journal-{when.strftime('%Y-%m')}.jsonl"

    def files(self) -> list[Path]:
        """All journal files, oldest first (plain and gzipped)."""
        if not self.directory.exists():
            return []
        out = [p for p in self.directory.iterdir() if p.name.startswith("journal-")
               and (p.name.endswith(".jsonl") or p.name.endswith(".jsonl.gz"))]
        return sorted(out, key=lambda p: p.name.replace(".gz", ""))

    # ------------------------------------------------------------------ write
    def append(self, kind: str, **fields: Any) -> int | None:
        """Append one record. Returns its sequence number, or None on failure.

        Never raises. A failure is counted in ``health()``.
        """
        try:
            with self._lock:
                now = self._clock()
                if now.tzinfo is None:
                    now = now.replace(tzinfo=timezone.utc)
                self.directory.mkdir(parents=True, exist_ok=True)
                if self._seq is None:
                    self._seq = self._last_seq()
                seq = self._seq + 1
                rec = {"seq": seq, "ts": now.isoformat(), "kind": str(kind), "v": SCHEMA_V}
                for k, v in fields.items():
                    if k in rec:
                        k = f"f_{k}"
                    rec[k] = v
                line = json.dumps(rec, default=_default, ensure_ascii=False, separators=(",", ":")) + "\n"
                path = self.path_for(now)
                data = line.encode("utf-8")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    written = 0
                    while written < len(data):
                        n = os.write(fd, data[written:])
                        if n <= 0:
                            raise OSError("short write to journal")
                        written += n
                    if self._fsync_all or kind in _DURABLE_KINDS:
                        os.fsync(fd)
                finally:
                    os.close(fd)
                self._seq = seq
                self._maybe_compress(now)
                self._health.ok = True
                return seq
        except Exception as e:  # never break trading
            self._health.ok = False
            self._health.errors += 1
            self._health.last_error = f"{type(e).__name__}: {e}"
            logger.error("journal append failed (%s): %s", kind, e)
            return None

    def _last_seq(self) -> int:
        files = self.files()
        for p in reversed(files):
            try:
                last = None
                opener = gzip.open if p.name.endswith(".gz") else open
                with opener(p, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            last = line
                if last:
                    return int(json.loads(last).get("seq", 0))
            except Exception as e:
                logger.warning("journal: could not read %s for seq: %s", p, e)
                continue
        return 0

    def _maybe_compress(self, now: datetime) -> None:
        current = self.path_for(now).name
        if self._compressed_upto == current:
            return
        for p in self.files():
            if p.name.endswith(".jsonl") and p.name < current:
                self._gzip(p)
        self._compressed_upto = current

    @staticmethod
    def _gzip(p: Path) -> None:
        gz = p.with_name(p.name + ".gz")
        tmp = gz.with_name(gz.name + ".tmp")
        with open(p, "rb") as src, gzip.open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, gz)
        p.unlink()

    # ------------------------------------------------------------------- read
    def read(self, kinds: set[str] | None = None, since: datetime | None = None) -> Iterator[dict]:
        """Iterate records oldest first, optionally filtered by kind and time."""
        since_s = since.isoformat() if since else None
        for p in self.files():
            opener = gzip.open if p.name.endswith(".gz") else open
            try:
                with opener(p, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue  # a torn last line after a crash is skipped
                        if kinds and rec.get("kind") not in kinds:
                            continue
                        if since_s and rec.get("ts", "") < since_s:
                            continue
                        yield rec
            except OSError as e:
                logger.warning("journal: cannot read %s: %s", p, e)

    def health(self) -> JournalHealth:
        used = 0
        for p in self.files():
            try:
                used += p.stat().st_size
            except OSError:
                pass
        self._health.bytes_used = used
        self._health.near_cap = self.max_bytes > 0 and used >= 0.8 * self.max_bytes
        return self._health
