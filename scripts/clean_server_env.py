#!/usr/bin/env python3
"""Safely remove retired legacy variables from the server `.env` file.

Safety guarantees:
- Standard library only (Python 3.12+).
- NEVER prints or logs any variable value or raw `.env` line (names, counts,
  and line numbers only).
- `--dry-run` is the default; `--apply` is required to touch disk.
- Removes ONLY an explicit hard-coded allowlist of 28 retired variable names
  (27 legacy Polymarket/NBA/NHL/Events bot names + `MAX_TOTAL_EXPOSURE_PCT`),
  plus `TELEGRAM_*` variables ONLY when their value is empty.
- Collapses exact duplicate assignments of kept variables, while failing closed
  if a kept variable has conflicting duplicate values.
- Refuses to modify anything (non-zero exit) on any syntax ambiguity or if any
  required runtime variable would be removed, missing, or empty afterwards.
- When `--apply` makes changes, writes a mode-`0600` timestamped sibling backup
  first and replaces `.env` atomically while preserving file mode and owner.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

# 27 legacy variable names from the retired Polymarket / NBA / NHL / Events bots
# (verified against env-summary.txt and docs/revamp/OWNER_CHECKLIST.md §3).
OLD_BOT_RETIRED_NAMES: tuple[str, ...] = (
    "TRADING_MODE",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "PRIVATE_KEY",
    "FUNDER_ADDRESS",
    "STARTING_BANKROLL",
    "MAX_BET_PCT",
    "MAX_GAME_EXPOSURE_PCT",
    "SCAN_INTERVAL",
    "EXIT_CHECK_INTERVAL",
    "PRE_TIPOFF_MINUTES",
    "MIN_GAME_EDGE",
    "MIN_FUTURES_EDGE",
    "ODDS_API_KEY",
    "BALLDONTLIE_API_KEY",
    "PROXY_HOST",
    "PROXY_PORT",
    "PROXY_USER",
    "PROXY_PASS",
    "NBA_SEASON",
    "LOG_LEVEL",
    "NHL_TRADING_MODE",
    "TWITTER_BEARER_TOKEN",
    "TIPRANKS_EMAIL",
    "TIPRANKS_PASSWORD",
    "EVENTS_STARTING_BANKROLL",
)

# Retired stock_agent variable replaced by MARGIN_ENABLED + MAX_GROSS_EXPOSURE_PCT.
RETIRED_STOCK_AGENT_NAMES: tuple[str, ...] = (
    "MAX_TOTAL_EXPOSURE_PCT",
)

UNCONDITIONAL_REMOVE_NAMES: frozenset[str] = frozenset(OLD_BOT_RETIRED_NAMES) | frozenset(
    RETIRED_STOCK_AGENT_NAMES
)

# Required runtime names that must never be removed and must remain non-empty.
REQUIRED_KEPT_NAMES: tuple[str, ...] = (
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "STOCK_AGENT_MODE",
    "DISCORD_BOT_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "PERPLEXITY_API_KEY",
    "FMP_API_KEY",
    "FINNHUB_API_KEY",
)

REQUIRED_KEPT_SET: frozenset[str] = frozenset(REQUIRED_KEPT_NAMES)

_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")


class EnvCleanError(Exception):
    """Raised on any ambiguity or safety-gate violation (message contains no values)."""


@dataclass(frozen=True)
class CleanPlan:
    cleaned_bytes: bytes
    changed: bool
    removed_retired: tuple[tuple[str, int], ...]
    removed_empty_telegram: tuple[tuple[str, int], ...]
    collapsed_duplicates: tuple[tuple[str, int], ...]
    kept_names: tuple[str, ...]


def _strip_line_ending(raw_line: str, *, line_no: int) -> str:
    if raw_line.endswith("\r\n"):
        body = raw_line[:-2]
    elif raw_line.endswith("\n"):
        body = raw_line[:-1]
    else:
        body = raw_line
    if "\r" in body:
        raise EnvCleanError(f"Line {line_no}: bare carriage return is not allowed")
    return body


def _parse_rhs_value(rhs: str, *, name: str, line_no: int) -> str:
    """Parse an assignment RHS in memory without ever including it in errors."""
    stripped = rhs.strip()
    if stripped == "" or stripped.startswith("#"):
        return ""

    quote = stripped[0]
    if quote in ('"', "'"):
        out_chars: list[str] = []
        idx = 1
        closed = False
        while idx < len(stripped):
            ch = stripped[idx]
            if quote == '"' and ch == "\\":
                if idx + 1 >= len(stripped):
                    raise EnvCleanError(
                        f"Line {line_no} ({name}): trailing escape in quoted value"
                    )
                out_chars.append(stripped[idx + 1])
                idx += 2
                continue
            if ch == quote:
                closed = True
                idx += 1
                break
            out_chars.append(ch)
            idx += 1

        if not closed:
            raise EnvCleanError(
                f"Line {line_no} ({name}): unclosed quote or multiline value"
            )
        trailing = stripped[idx:].strip()
        if trailing and not trailing.startswith("#"):
            raise EnvCleanError(
                f"Line {line_no} ({name}): unexpected characters after quoted value"
            )
        return "".join(out_chars)

    uncommented = _INLINE_COMMENT_RE.sub("", stripped).rstrip()
    if uncommented.endswith("\\"):
        raise EnvCleanError(
            f"Line {line_no} ({name}): line-continuation backslash is not supported"
        )
    return uncommented


def _increment(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def plan_env_clean(raw_bytes: bytes, *, _verify_idempotent: bool = True) -> CleanPlan:
    """Compute and validate a lossless cleaning plan from raw `.env` bytes."""
    if UNCONDITIONAL_REMOVE_NAMES & REQUIRED_KEPT_SET:
        overlap = sorted(UNCONDITIONAL_REMOVE_NAMES & REQUIRED_KEPT_SET)
        raise EnvCleanError(f"Internal allowlist conflict on required names: {overlap}")

    if b"\x00" in raw_bytes:
        raise EnvCleanError("Refusing to parse file containing NUL bytes")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvCleanError("Refusing to parse non-UTF-8 `.env` file") from exc

    if text.startswith("\ufeff"):
        raise EnvCleanError("Refusing to parse UTF-8 BOM-prefixed `.env` file")

    lines = text.splitlines(keepends=True)
    if not lines:
        raise EnvCleanError("Target `.env` file is empty")

    kept_lines: list[str] = []
    seen_kept: dict[str, tuple[str, int]] = {}
    seen_empty_telegram: dict[str, int] = {}
    removed_retired: dict[str, int] = {}
    removed_empty_telegram: dict[str, int] = {}
    collapsed_duplicates: dict[str, int] = {}

    for line_no, raw_line in enumerate(lines, start=1):
        body = _strip_line_ending(raw_line, line_no=line_no)
        trimmed = body.strip()

        if trimmed == "" or trimmed.startswith("#"):
            kept_lines.append(raw_line)
            continue

        match = _ASSIGNMENT_RE.match(body)
        if match is None:
            raise EnvCleanError(
                f"Line {line_no}: unrecognized syntax (expected KEY=VALUE, comment, or blank line)"
            )

        name, rhs = match.group(1), match.group(2)
        value = _parse_rhs_value(rhs, name=name, line_no=line_no)

        if name in UNCONDITIONAL_REMOVE_NAMES:
            if name in REQUIRED_KEPT_SET:
                raise EnvCleanError(f"Refusing to remove required variable: {name}")
            _increment(removed_retired, name)
            continue

        if name.startswith("TELEGRAM_"):
            if name in REQUIRED_KEPT_SET:
                raise EnvCleanError(f"Refusing to remove required variable: {name}")
            if value.strip() == "":
                if name in seen_kept:
                    prev_line = seen_kept[name][1]
                    raise EnvCleanError(
                        f"Conflicting duplicate assignments for {name} "
                        f"(lines {prev_line} and {line_no})"
                    )
                seen_empty_telegram.setdefault(name, line_no)
                _increment(removed_empty_telegram, name)
                continue
            if name in seen_empty_telegram:
                prev_line = seen_empty_telegram[name]
                raise EnvCleanError(
                    f"Conflicting duplicate assignments for {name} "
                    f"(lines {prev_line} and {line_no})"
                )

        if name in seen_kept:
            prev_val, prev_line = seen_kept[name]
            if prev_val != value:
                raise EnvCleanError(
                    f"Conflicting duplicate assignments for kept variable {name} "
                    f"(lines {prev_line} and {line_no})"
                )
            _increment(collapsed_duplicates, name)
            continue

        seen_kept[name] = (value, line_no)
        kept_lines.append(raw_line)

    missing_required = [name for name in REQUIRED_KEPT_NAMES if name not in seen_kept]
    if missing_required:
        raise EnvCleanError(
            f"Missing required variable(s) after cleaning: {', '.join(missing_required)}"
        )

    empty_required = [
        name for name in REQUIRED_KEPT_NAMES if seen_kept[name][0].strip() == ""
    ]
    if empty_required:
        raise EnvCleanError(
            f"Required variable(s) have empty values: {', '.join(empty_required)}"
        )

    if kept_lines and not kept_lines[-1].endswith("\n"):
        newline = "\r\n" if "\r\n" in text else "\n"
        kept_lines[-1] = kept_lines[-1] + newline

    cleaned_bytes = "".join(kept_lines).encode("utf-8")
    changed = cleaned_bytes != raw_bytes

    if _verify_idempotent:
        second_pass = plan_env_clean(cleaned_bytes, _verify_idempotent=False)
        if second_pass.changed or second_pass.cleaned_bytes != cleaned_bytes:
            raise EnvCleanError("Idempotency self-check failed; refusing to proceed")

    return CleanPlan(
        cleaned_bytes=cleaned_bytes,
        changed=changed,
        removed_retired=tuple(removed_retired.items()),
        removed_empty_telegram=tuple(removed_empty_telegram.items()),
        collapsed_duplicates=tuple(collapsed_duplicates.items()),
        kept_names=tuple(seen_kept.keys()),
    )


def _fsync_directory(directory: Path) -> None:
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _write_backup(env_path: Path, raw_bytes: bytes, st: os.stat_result) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    parent = env_path.parent
    candidate = parent / f"{env_path.name}.bak.{stamp}"
    suffix = 1
    while candidate.exists():
        candidate = parent / f"{env_path.name}.bak.{stamp}.{suffix}"
        suffix += 1

    fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        if st.st_uid != os.geteuid() or st.st_gid != os.getegid():
            os.fchown(fd, st.st_uid, st.st_gid)
        view = memoryview(raw_bytes)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            candidate.unlink()
        except OSError:
            pass
        raise
    else:
        os.close(fd)

    _fsync_directory(parent)
    return candidate


def _atomic_replace_preserving_metadata(
    env_path: Path, cleaned_bytes: bytes, st: os.stat_result
) -> None:
    parent = env_path.parent
    file_mode = stat.S_IMODE(st.st_mode)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{env_path.name}.tmp.",
        dir=parent,
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, file_mode)
        if st.st_uid != os.geteuid() or st.st_gid != os.getegid():
            os.fchown(fd, st.st_uid, st.st_gid)
        view = memoryview(cleaned_bytes)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, env_path)
    except BaseException:
        if fd != -1:
            os.close(fd)
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise

    _fsync_directory(parent)


def _format_counts(items: tuple[tuple[str, int], ...]) -> str:
    if not items:
        return "(none)"
    return ", ".join(f"{name} (x{count})" if count > 1 else name for name, count in items)


def run(env_path: Path, *, apply_changes: bool) -> int:
    if env_path.is_symlink():
        raise EnvCleanError(f"Refusing to operate on symlink: {env_path}")
    if not env_path.exists():
        raise EnvCleanError(f"Target file does not exist: {env_path}")

    st = os.stat(env_path, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        raise EnvCleanError(f"Target path is not a regular file: {env_path}")

    raw_bytes = env_path.read_bytes()
    plan = plan_env_clean(raw_bytes)

    mode_label = "APPLY" if apply_changes else "DRY-RUN"
    print(f"[{mode_label}] Target: {env_path}")
    print(f"  Removed retired variables: {_format_counts(plan.removed_retired)}")
    print(
        f"  Removed empty TELEGRAM_* variables: "
        f"{_format_counts(plan.removed_empty_telegram)}"
    )
    print(
        f"  Collapsed exact duplicate kept variables: "
        f"{_format_counts(plan.collapsed_duplicates)}"
    )
    print(f"  Kept variables ({len(plan.kept_names)}): {', '.join(plan.kept_names)}")
    print(
        f"  Verified required non-empty variables ({len(REQUIRED_KEPT_NAMES)}): "
        f"{', '.join(REQUIRED_KEPT_NAMES)}"
    )

    if not apply_changes:
        if plan.changed:
            print("  Result: changes planned (re-run with --apply to write backup and update file).")
        else:
            print("  Result: file already clean (no changes needed).")
        return 0

    if not plan.changed:
        print("  Result: file already clean; nothing modified.")
        return 0

    backup_path = _write_backup(env_path, raw_bytes, st)
    _atomic_replace_preserving_metadata(env_path, plan.cleaned_bytes, st)
    print(f"  Backup written (mode 0600): {backup_path}")
    print("  Result: updated `.env` atomically (mode, owner, comments, and key order preserved).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Remove retired legacy variables from a `.env` file without printing any values."
        )
    )
    parser.add_argument(
        "env_path",
        type=Path,
        help="Path to the target `.env` file (e.g. /root/polymarket-bot/.env).",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview variable names to remove/keep without touching disk (default).",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="Create a mode-0600 timestamped sibling backup and atomically update `.env`.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args.env_path, apply_changes=bool(args.apply))
    except EnvCleanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"ERROR: filesystem failure ({type(exc).__name__}: errno={exc.errno})",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
