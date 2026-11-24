"""Local TipRanks screener CSV ingestion, freshness tracking, health, and research hooks.

Architecture contract:
- The trading bot NEVER connects to TipRanks over HTTP or stores TipRanks credentials.
- An external scheduled job atomically drops a CSV export inside ``DATA_DIR``
  (default ``tipranks_latest.csv``).
- Freshness bands:
  - ``ok``: age <= 8 days
  - ``stale``: 8 days < age <= 15 days (still usable, flagged stale)
  - ``dead``: age > 15 days, malformed, symlink/outside ``DATA_DIR``, or 0 valid rows (never used)
  - ``absent``: file does not exist (clean no-op, never pages)
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from stock_agent.asset_registry import AssetRegistry, CanonicalSector
from stock_agent.config import Config
from stock_agent.health import HealthState, ProviderHealthRegistry
from stock_agent.research_schemas import Evidence, utc_datetime
from stock_agent.risk_manager import _LEVERAGED_2X, _LEVERAGED_3X, normalize_sector

logger = logging.getLogger(__name__)

DEFAULT_TIPRANKS_FILENAME = "tipranks_latest.csv"
FRESH_MAX_AGE = timedelta(days=8)
STALE_MAX_AGE = timedelta(days=15)
DEFAULT_MAX_CSV_BYTES = 5 * 1024 * 1024  # 5 MiB ceiling (~80x a 300-row export)
_MAX_SIDECAR_BYTES = 16 * 1024

_REQUIRED_COLUMNS = frozenset({"Ticker", "Smart Score", "Analyst Consensus"})
_TICKER_RE = re.compile(r"^[A-Z]{1,6}(?:\.[A-Z])?$")
_MUTUAL_FUND_5X_RE = re.compile(r"^[A-Z]{4}X$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")

_CONSENSUS_ALLOWLIST: dict[str, str] = {
    "strong buy": "Strong Buy",
    "moderate buy": "Moderate Buy",
    "hold": "Hold",
    "moderate sell": "Moderate Sell",
    "strong sell": "Strong Sell",
}

_ELIGIBLE_UNIVERSE_CONSENSUS = frozenset({"Strong Buy", "Moderate Buy"})

_KNOWN_ETF_OR_FUND_TICKERS: frozenset[str] = (
    frozenset(
        {
            "SPY",
            "QQQ",
            "IWM",
            "DIA",
            "VOO",
            "VTI",
            "IVV",
            "RSP",
            "MDY",
            "TLT",
            "IEF",
            "SHY",
            "BND",
            "AGG",
            "HYG",
            "LQD",
            "GLD",
            "IAU",
            "SLV",
            "USO",
            "UNG",
            "DBC",
            "VIXY",
            "VXX",
            "UVXY",
            "SVXY",
            "ARKK",
            "ARKW",
            "ARKG",
            "SMH",
            "SOXX",
            "IBB",
            "XBI",
            "KRE",
            "XHB",
            "GDX",
            "XLF",
            "XLK",
            "XLE",
            "XLV",
            "XLI",
            "XLP",
            "XLY",
            "XLU",
            "XLB",
            "XLRE",
            "XLC",
        }
    )
    | _LEVERAGED_2X
    | _LEVERAGED_3X
)

_FUND_NAME_RE = re.compile(
    r"\b(?:ETF|ETN|ETFs|ETNs|Mutual\s+Fund|Index\s+Fund|Exchange[\s-]+Traded|UCITS|"
    r"ProShares|Direxion|iShares|SPDR)\b",
    re.IGNORECASE,
)

_NULL_TOKENS = frozenset({"", "-", "--", "n/a", "na", "nan", "null", "none", "unknown"})
_MAGNITUDE_MULTIPLIERS = {
    "K": 1e3,
    "M": 1e6,
    "B": 1e9,
    "T": 1e12,
}


@dataclass(frozen=True)
class TipRanksRow:
    symbol: str
    name: str
    price: float | None
    change_pct: float | None
    volume: float | None
    avg_volume_3m: float | None
    target_upside_pct: float | None
    price_target: float | None
    analyst_consensus: str | None
    smart_score: int | None
    market_cap: float | None
    pe_ratio: float | None
    dividend_yield_pct: float | None
    sector: str


@dataclass(frozen=True)
class TipRanksSnapshot:
    state: HealthState
    rows: dict[str, TipRanksRow]
    exported_at: datetime | None
    age_days: float | None
    content_sha256: str | None = None
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.state in (HealthState.OK, HealthState.STALE) and bool(self.rows)

    @property
    def stale(self) -> bool:
        return self.state is HealthState.STALE


@dataclass(frozen=True)
class _CachedParse:
    file_sig: tuple[int, int, int, int]
    sidecar_sig: tuple[int, int, int, int] | None
    content_sha256: str
    rows: dict[str, TipRanksRow]
    file_mtime: datetime
    claimed_exported_at: datetime | None
    parse_error: str | None = None


def is_fund_ticker(symbol: str, name: str | None = None) -> bool:
    """Return True for mutual funds, ETFs, ETNs, and leveraged/index products."""
    sym = (symbol or "").strip().upper().replace("-", ".")
    if not sym:
        return True
    if sym in _KNOWN_ETF_OR_FUND_TICKERS:
        return True
    if _MUTUAL_FUND_5X_RE.match(sym):
        return True
    if name and _FUND_NAME_RE.search(name):
        return True
    return False


def _clean_text(raw: str | None, *, max_len: int) -> str:
    if not raw:
        return ""
    cleaned = _CONTROL_CHARS_RE.sub(" ", str(raw)).strip()
    return cleaned[:max_len]


def _normalize_ticker(raw: str | None) -> str | None:
    if not raw:
        return None
    sym = _CONTROL_CHARS_RE.sub("", str(raw)).strip().upper().replace("-", ".")
    if not _TICKER_RE.match(sym):
        return None
    return sym


def _parse_number(
    raw: str | None,
    *,
    strip_chars: str = "$,% ",
    allow_negative: bool = True,
    require_positive: bool = False,
) -> float | None:
    if raw is None:
        return None
    token = str(raw).strip()
    if token.lower() in _NULL_TOKENS:
        return None
    cleaned = token.replace(",", "").strip()
    for ch in strip_chars:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()
    if not cleaned or cleaned.lower() in _NULL_TOKENS:
        return None
    multiplier = 1.0
    suffix = cleaned[-1].upper()
    if suffix in _MAGNITUDE_MULTIPLIERS:
        multiplier = _MAGNITUDE_MULTIPLIERS[suffix]
        cleaned = cleaned[:-1].strip()
    try:
        val = float(cleaned) * multiplier
    except ValueError:
        return None
    if not math.isfinite(val):
        return None
    if not allow_negative and val < 0.0:
        return None
    if require_positive and val <= 0.0:
        return None
    return val


def _parse_smart_score(raw: str | None) -> int | None:
    """Parse TipRanks Smart Score (integer 1..10). Missing or invalid -> None, NEVER 0."""
    val = _parse_number(raw, strip_chars=" ", allow_negative=False, require_positive=True)
    if val is None:
        return None
    rounded = int(round(val))
    if abs(val - rounded) > 1e-6:
        return None
    if not (1 <= rounded <= 10):
        return None
    return rounded


def _parse_consensus(raw: str | None) -> str | None:
    if raw is None:
        return None
    norm = " ".join(_CONTROL_CHARS_RE.sub(" ", str(raw)).strip().lower().split())
    return _CONSENSUS_ALLOWLIST.get(norm)


def _parse_export_timestamp(raw: Any) -> datetime | None:
    if raw is None or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            d = date.fromisoformat(text)
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        return utc_datetime(text)
    except Exception:
        return None


def _resolve_confined_path(data_dir: Path | str, filename: str) -> tuple[Path | None, str | None]:
    if not filename or not isinstance(filename, str):
        return None, "invalid TipRanks filename"
    if "/" in filename or "\\" in filename or "\x00" in filename or filename in {".", ".."}:
        return None, "TipRanks path escapes DATA_DIR"
    rel = Path(filename)
    if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 1 or filename != rel.name:
        return None, "TipRanks path escapes DATA_DIR"
    raw_dir = Path(data_dir)
    try:
        if raw_dir.is_symlink():
            return None, "DATA_DIR must not be a symlink"
        base = raw_dir.resolve(strict=False)
        target = base / rel.name
        resolved_parent = target.parent.resolve(strict=False)
        if resolved_parent != base:
            return None, "TipRanks path outside DATA_DIR"
    except OSError as exc:
        return None, f"cannot resolve DATA_DIR ({type(exc).__name__})"
    return target, None


def _stat_sig(st: os.stat_result) -> tuple[int, int, int, int]:
    mtime_ns = st.st_mtime_ns if st.st_mtime_ns is not None else int(round(st.st_mtime * 1_000_000_000))
    return (int(st.st_dev), int(st.st_ino), int(st.st_size), int(mtime_ns))


def _safe_lstat(path: Path) -> tuple[os.stat_result | None, str | None, bool]:
    """Return (lstat_result, error_reason, is_absent) without following symlinks."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, None, True
    except OSError as exc:
        return None, f"cannot stat file ({type(exc).__name__})", False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None, "refused symlink or non-regular file", False
    return st, None, False


def _read_regular_file_bytes(
    path: Path,
    *,
    max_bytes: int,
    initial_lstat: os.stat_result | None = None,
) -> tuple[bytes | None, os.stat_result | None, str | None, bool]:
    """Read a regular file inside DATA_DIR without following symlinks and detect mid-read swaps."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    lst_before = initial_lstat

    for attempt in range(2):
        if lst_before is None or attempt > 0:
            lst_before, err, absent = _safe_lstat(path)
            if absent:
                return None, None, None, True
            if err or lst_before is None:
                return None, None, err or "unreadable file", False

        if lst_before.st_size <= 0:
            return None, None, "empty CSV file", False
        if lst_before.st_size > max_bytes:
            return None, None, f"CSV exceeds max size ({lst_before.st_size} bytes)", False

        dir_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        dir_fd = -1
        try:
            dir_fd = os.open(path.parent, dir_flags)
            fd = os.open(path.name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            return None, None, None, True
        except OSError as exc:
            return None, None, f"refused open ({type(exc).__name__})", False
        finally:
            if dir_fd >= 0:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass

        try:
            fst_open = os.fstat(fd)
            if not stat.S_ISREG(fst_open.st_mode):
                return None, None, "refused non-regular file descriptor", False
            if (fst_open.st_dev, fst_open.st_ino) != (lst_before.st_dev, lst_before.st_ino):
                lst_before = None
                continue
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                data = handle.read(max_bytes + 1)
                fst_after = os.fstat(handle.fileno())
        except OSError as exc:
            return None, None, f"read error ({type(exc).__name__})", False
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

        if len(data) > max_bytes:
            return None, None, f"CSV exceeds max size ({len(data)} bytes)", False

        lst_after, err_after, absent_after = _safe_lstat(path)
        if absent_after or err_after or lst_after is None:
            lst_before = None
            continue
        if (
            _stat_sig(lst_after) != _stat_sig(fst_open)
            or _stat_sig(fst_after) != _stat_sig(fst_open)
            or len(data) != fst_after.st_size
        ):
            lst_before = None
            continue

        return data, fst_after, None, False

    return None, None, "file modified mid-read", False


def _parse_csv_bytes(raw: bytes) -> tuple[dict[str, TipRanksRow], datetime | None, str | None]:
    if b"\x00" in raw:
        return {}, None, "binary NUL byte in CSV"
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return {}, None, "invalid UTF-8 encoding in CSV"

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {}, None, "missing CSV header"

    header_map: dict[str, str] = {}
    for col in reader.fieldnames:
        if col is None:
            continue
        clean_col = col.lstrip("\ufeff").strip()
        if clean_col and clean_col not in header_map:
            header_map[clean_col] = col

    missing_cols = sorted(_REQUIRED_COLUMNS - header_map.keys())
    if missing_cols:
        return {}, None, f"missing required columns: {', '.join(missing_cols)}"

    def _cell(row_dict: dict[str, Any], col_name: str) -> str | None:
        raw_key = header_map.get(col_name)
        if raw_key is None:
            return None
        val = row_dict.get(raw_key)
        return val if isinstance(val, str) else None

    rows: dict[str, TipRanksRow] = {}
    embedded_exported_at: datetime | None = None

    try:
        for raw_row in reader:
            if None in raw_row and raw_row[None]:
                # Extra unquoted delimiters in row; skip malformed row safely
                continue
            symbol = _normalize_ticker(_cell(raw_row, "Ticker"))
            if symbol is None or symbol in rows:
                continue

            if embedded_exported_at is None:
                for ts_col in ("Exported At", "Export Date", "As Of"):
                    ts_val = _parse_export_timestamp(_cell(raw_row, ts_col))
                    if ts_val is not None:
                        embedded_exported_at = ts_val
                        break

            rows[symbol] = TipRanksRow(
                symbol=symbol,
                name=_clean_text(_cell(raw_row, "Name"), max_len=120) or symbol,
                price=_parse_number(_cell(raw_row, "Price"), require_positive=True),
                change_pct=_parse_number(_cell(raw_row, "Change %")),
                volume=_parse_number(_cell(raw_row, "Volume"), allow_negative=False),
                avg_volume_3m=_parse_number(_cell(raw_row, "Avg. Volume (3M)"), allow_negative=False),
                target_upside_pct=_parse_number(_cell(raw_row, "Analyst Price Target %")),
                price_target=_parse_number(_cell(raw_row, "Analyst Price Target"), require_positive=True),
                analyst_consensus=_parse_consensus(_cell(raw_row, "Analyst Consensus")),
                smart_score=_parse_smart_score(_cell(raw_row, "Smart Score")),
                market_cap=_parse_number(_cell(raw_row, "Market Cap"), allow_negative=False),
                pe_ratio=_parse_number(_cell(raw_row, "P/E Ratio")),
                dividend_yield_pct=_parse_number(_cell(raw_row, "Dividend Yield %"), allow_negative=False),
                sector=_clean_text(_cell(raw_row, "Sector"), max_len=64),
            )
    except csv.Error as exc:
        return {}, None, f"CSV parse error ({type(exc).__name__})"

    if not rows:
        return {}, None, "0 valid rows in CSV"
    return rows, embedded_exported_at, None


class TipRanksIngestor:
    """Stat-cached reader for the local TipRanks screener export."""

    def __init__(
        self,
        config: Any = Config,
        *,
        health: ProviderHealthRegistry | None = None,
        filename: str = DEFAULT_TIPRANKS_FILENAME,
        max_bytes: int = DEFAULT_MAX_CSV_BYTES,
    ) -> None:
        self.config = config
        self.health = health
        self.filename = filename
        self.max_bytes = int(max_bytes)
        self._cache: _CachedParse | None = None

    def _sidecar_name(self) -> str:
        stem = Path(self.filename).stem or "tipranks_latest"
        return f"{stem}.meta.json"

    def _read_sidecar_timestamp(
        self,
        data_dir: Path | str,
    ) -> tuple[datetime | None, tuple[int, int, int, int] | None]:
        sidecar_path, err = _resolve_confined_path(data_dir, self._sidecar_name())
        if err or sidecar_path is None:
            return None, None
        st, stat_err, absent = _safe_lstat(sidecar_path)
        if absent or stat_err or st is None:
            return None, None
        raw, st_after, read_err, read_absent = _read_regular_file_bytes(
            sidecar_path,
            max_bytes=_MAX_SIDECAR_BYTES,
            initial_lstat=st,
        )
        if read_absent or read_err or raw is None or st_after is None:
            return None, None
        try:
            payload = json.loads(raw.decode("utf-8", errors="strict"))
        except Exception:
            return None, _stat_sig(st_after)
        if not isinstance(payload, dict):
            return None, _stat_sig(st_after)
        for key in ("exported_at", "as_of", "export_date"):
            parsed = _parse_export_timestamp(payload.get(key))
            if parsed is not None:
                return parsed, _stat_sig(st_after)
        return None, _stat_sig(st_after)

    def _record_health(
        self,
        state: HealthState,
        *,
        row_count: int,
        age_days: float | None,
        summary: str,
        at: datetime,
    ) -> None:
        if self.health is None:
            return
        self.health.record_tipranks(
            state,
            row_count=row_count,
            age_days=age_days,
            summary=summary,
            at=at,
        )

    def read(self, now: datetime) -> TipRanksSnapshot:
        """Read (or stat-validate cache of) the local TipRanks CSV and update health."""
        now_utc = utc_datetime(now)
        if not bool(getattr(self.config, "TIPRANKS_ENABLED", True)):
            self._record_health(
                HealthState.DISABLED,
                row_count=0,
                age_days=None,
                summary="disabled via TIPRANKS_ENABLED=false",
                at=now_utc,
            )
            return TipRanksSnapshot(
                state=HealthState.DISABLED,
                rows={},
                exported_at=None,
                age_days=None,
                reason="disabled",
            )

        data_dir = getattr(self.config, "DATA_DIR", Config.DATA_DIR)
        csv_path, path_err = _resolve_confined_path(data_dir, self.filename)
        if path_err or csv_path is None:
            reason = path_err or "invalid TipRanks path"
            self._record_health(
                HealthState.DEAD,
                row_count=0,
                age_days=None,
                summary=f"0 rows ({reason})",
                at=now_utc,
            )
            return TipRanksSnapshot(
                state=HealthState.DEAD,
                rows={},
                exported_at=None,
                age_days=None,
                reason=reason,
            )

        lst, stat_err, is_absent = _safe_lstat(csv_path)
        if is_absent:
            self._cache = None
            self._record_health(
                HealthState.ABSENT,
                row_count=0,
                age_days=None,
                summary="0 rows, file absent",
                at=now_utc,
            )
            return TipRanksSnapshot(
                state=HealthState.ABSENT,
                rows={},
                exported_at=None,
                age_days=None,
                reason="file absent",
            )
        if stat_err or lst is None:
            self._cache = None
            reason = stat_err or "unreadable TipRanks CSV"
            self._record_health(
                HealthState.DEAD,
                row_count=0,
                age_days=None,
                summary=f"0 rows ({reason})",
                at=now_utc,
            )
            return TipRanksSnapshot(
                state=HealthState.DEAD,
                rows={},
                exported_at=None,
                age_days=None,
                reason=reason,
            )

        sidecar_dt, sidecar_sig = self._read_sidecar_timestamp(data_dir)
        cur_sig = _stat_sig(lst)

        cached = self._cache
        if cached is None or cached.file_sig != cur_sig or cached.sidecar_sig != sidecar_sig:
            raw, fst, read_err, read_absent = _read_regular_file_bytes(
                csv_path,
                max_bytes=self.max_bytes,
                initial_lstat=lst,
            )
            if read_absent:
                self._cache = None
                self._record_health(
                    HealthState.ABSENT,
                    row_count=0,
                    age_days=None,
                    summary="0 rows, file absent",
                    at=now_utc,
                )
                return TipRanksSnapshot(
                    state=HealthState.ABSENT,
                    rows={},
                    exported_at=None,
                    age_days=None,
                    reason="file absent",
                )
            if read_err or raw is None or fst is None:
                self._cache = None
                reason = read_err or "unreadable TipRanks CSV"
                self._record_health(
                    HealthState.DEAD,
                    row_count=0,
                    age_days=None,
                    summary=f"0 rows ({reason})",
                    at=now_utc,
                )
                return TipRanksSnapshot(
                    state=HealthState.DEAD,
                    rows={},
                    exported_at=None,
                    age_days=None,
                    reason=reason,
                )

            digest = hashlib.sha256(raw).hexdigest()
            parsed_rows, embedded_dt, parse_err = _parse_csv_bytes(raw)
            claimed_dt = sidecar_dt or embedded_dt
            mtime_dt = datetime.fromtimestamp(fst.st_mtime, tz=timezone.utc)
            cached = _CachedParse(
                file_sig=_stat_sig(fst),
                sidecar_sig=sidecar_sig,
                content_sha256=digest,
                rows=parsed_rows,
                file_mtime=mtime_dt,
                claimed_exported_at=claimed_dt,
                parse_error=parse_err,
            )
            self._cache = cached

        if cached.parse_error is not None:
            self._record_health(
                HealthState.DEAD,
                row_count=0,
                age_days=None,
                summary=f"0 rows ({cached.parse_error})",
                at=now_utc,
            )
            return TipRanksSnapshot(
                state=HealthState.DEAD,
                rows={},
                exported_at=None,
                age_days=None,
                content_sha256=cached.content_sha256,
                reason=cached.parse_error,
            )

        effective_mtime = min(cached.file_mtime, now_utc)
        effective_as_of = effective_mtime
        if cached.claimed_exported_at is not None and cached.claimed_exported_at <= now_utc + timedelta(hours=1):
            effective_as_of = min(effective_mtime, min(cached.claimed_exported_at, now_utc))

        age = max(timedelta(0), now_utc - effective_as_of)
        age_days = age.total_seconds() / 86400.0
        row_count = len(cached.rows)

        if age <= FRESH_MAX_AGE:
            state = HealthState.OK
            summary = f"{row_count} rows, age {age_days:.1f}d (exported {effective_as_of.date().isoformat()})"
            rows_out = cached.rows
        elif age <= STALE_MAX_AGE:
            state = HealthState.STALE
            summary = f"{row_count} rows, age {age_days:.1f}d (stale >8d, exported {effective_as_of.date().isoformat()})"
            rows_out = cached.rows
        else:
            state = HealthState.DEAD
            summary = f"0 usable rows, age {age_days:.1f}d (dead >15d, exported {effective_as_of.date().isoformat()})"
            rows_out = {}

        self._record_health(
            state,
            row_count=row_count if state is not HealthState.DEAD else 0,
            age_days=age_days,
            summary=summary,
            at=now_utc,
        )
        return TipRanksSnapshot(
            state=state,
            rows=rows_out,
            exported_at=effective_as_of,
            age_days=age_days,
            content_sha256=cached.content_sha256,
            reason=summary,
        )

    def universe_cache_key(self, now: datetime) -> tuple[Any, ...]:
        """Identity key for the 12h universe cache so new or expired exports take effect immediately."""
        if not bool(getattr(self.config, "TIPRANKS_ENABLED", True)):
            return ("disabled", int(getattr(self.config, "UNIVERSE_SIZE", 50)))
        snap = self.read(now)
        return (
            snap.state.value if snap.usable else "unusable",
            snap.content_sha256 if snap.usable else None,
            int(getattr(self.config, "TIPRANKS_MIN_SMART_SCORE", 8)),
            int(getattr(self.config, "TIPRANKS_UNIVERSE_SLICE", 20)),
            int(getattr(self.config, "TIPRANKS_UNIVERSE_LIMIT", 100)),
            int(getattr(self.config, "UNIVERSE_SIZE", 50)),
        )

    def evidence_for(self, symbol: str, now: datetime) -> tuple[Evidence, ...]:
        """Return sanitized Option A Evidence item for ``symbol`` when present in a fresh/stale CSV."""
        norm_sym = _normalize_ticker(symbol)
        if norm_sym is None:
            return ()
        snap = self.read(now)
        if not snap.usable or snap.exported_at is None:
            return ()
        row = snap.rows.get(norm_sym)
        if row is None:
            return ()

        now_utc = utc_datetime(now)
        published_at = min(snap.exported_at, now_utc)
        export_date_iso = published_at.date().isoformat()
        stale_tag = "; STALE >8d" if snap.stale else ""
        score_str = f"Smart Score {row.smart_score}/10" if row.smart_score is not None else "Smart Score unknown"
        consensus_str = (
            f"Analyst Consensus {row.analyst_consensus}"
            if row.analyst_consensus is not None
            else "Analyst Consensus unknown"
        )
        if row.target_upside_pct is not None:
            target_str = f" (target ${row.price_target:.2f})" if row.price_target is not None else ""
            upside_str = f"Price Target Upside {row.target_upside_pct:+.2f}%{target_str}"
        else:
            upside_str = "Price Target Upside unknown"

        snippet = (
            f"TipRanks screener export ({export_date_iso}{stale_tag}): "
            f"{score_str}; {consensus_str}; {upside_str}."
        )
        title = f"TipRanks screener snapshot for {norm_sym} ({export_date_iso})"
        return (
            Evidence(
                evidence_id=f"tipranks:{export_date_iso}:{norm_sym}",
                url=f"https://www.tipranks.com/stocks/{norm_sym.lower()}/forecast",
                title=title,
                published_at=published_at,
                retrieved_at=now_utc,
                publisher="TipRanks Screener Export",
                snippet=snippet,
            ),
        )

    def universe_candidates(
        self,
        now: datetime,
        *,
        assets: AssetRegistry,
        exclude: Iterable[str] = (),
        max_names: int | None = None,
    ) -> list[str]:
        """Return Option B TipRanks universe candidates sorted by Smart Score desc then Market Cap desc."""
        snap = self.read(now)
        if not snap.usable:
            return []

        min_score = int(getattr(self.config, "TIPRANKS_MIN_SMART_SCORE", 8))
        hard_limit = max(0, int(getattr(self.config, "TIPRANKS_UNIVERSE_LIMIT", 100)))
        cap = min(hard_limit, max(0, int(max_names))) if max_names is not None else hard_limit
        if cap <= 0:
            return []

        excluded = {s.strip().upper() for s in exclude if s}
        eligible: list[tuple[TipRanksRow, str]] = []
        for row in snap.rows.values():
            if row.smart_score is None or row.smart_score < min_score:
                continue
            if row.analyst_consensus not in _ELIGIBLE_UNIVERSE_CONSENSUS:
                continue
            if is_fund_ticker(row.symbol, row.name):
                continue
            resolved = assets.resolve_symbol(row.symbol)
            if not resolved or resolved in excluded:
                continue
            meta = assets.get_asset_meta(resolved)
            if not meta.tradable or meta.renamed_to:
                continue
            if is_fund_ticker(resolved, meta.company_name):
                continue
            if meta.canonical_sector == CanonicalSector.UNKNOWN and row.sector:
                norm_sec = normalize_sector(row.sector)
                if norm_sec != CanonicalSector.UNKNOWN:
                    assets.set_sector(resolved, row.sector)
            eligible.append((row, resolved))

        eligible.sort(
            key=lambda item: (
                -(item[0].smart_score or 0),
                -(item[0].market_cap if item[0].market_cap is not None else -1.0),
                item[1],
            )
        )

        out: list[str] = []
        seen = set(excluded)
        for _row, resolved in eligible:
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(resolved)
            if len(out) >= cap:
                break
        return out
