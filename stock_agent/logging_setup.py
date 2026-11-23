"""Central logging setup with secret redaction, httpx clamping, and rotating logs."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from stock_agent.config import Config, Settings

DEFAULT_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
DEFAULT_LOG_MAX_BYTES = 10_000_000
DEFAULT_LOG_BACKUP_COUNT = 12

_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:apikey|api_key|token|access_token|secret|password|auth|key)=)([^&\s'\"<>]+)"
)
_BARE_KV_SECRET_RE = re.compile(
    r"(?i)(\b(?:apikey|api_key|access_token|x-finnhub-token|x-apikey|x-apitoken|"
    r"apca-api-secret-key|apca-api-key-id|password|secret)\s*[:=]\s*['\"]?)([^\s,'\"<>};]+)"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|bot|token|basic)\s+)([^\s,'\"<>};]+)"
)
_BEARER_TOKEN_RE = re.compile(
    r"(?i)(\b(?:bearer|bot)\s+)([A-Za-z0-9._~+/=-]{8,})"
)
_TELEGRAM_BOT_PATH_RE = re.compile(r"(/bot\d+:)[A-Za-z0-9_-]+")


def _normalize_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    unique = {s for s in secrets if isinstance(s, str) and len(s) >= 4}
    # Longest first so prefix substrings do not leave trailing secret characters.
    return tuple(sorted(unique, key=len, reverse=True))


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Scrub URL query/path secrets, auth headers, and configured secret literals."""
    if not text:
        return text
    cleaned = _TELEGRAM_BOT_PATH_RE.sub(r"\1[REDACTED]", text)
    cleaned = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", cleaned)
    cleaned = _AUTH_HEADER_RE.sub(r"\1[REDACTED]", cleaned)
    cleaned = _BARE_KV_SECRET_RE.sub(r"\1[REDACTED]", cleaned)
    cleaned = _BEARER_TOKEN_RE.sub(r"\1[REDACTED]", cleaned)
    for secret in _normalize_secrets(secrets):
        if secret in cleaned:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


class SecretRedactingFilter(logging.Filter):
    """Filter that scrubs rendered log messages (including Exception args) in place."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = _normalize_secrets(secrets)

    def set_secrets(self, secrets: Iterable[str]) -> None:
        self._secrets = _normalize_secrets(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001
            rendered = str(record.msg)
        record.msg = redact_text(rendered, self._secrets)
        record.args = ()
        return True


class SecretRedactingFormatter(logging.Formatter):
    """Formatter that scrubs formatted output including tracebacks (`exc_info`)."""

    def __init__(
        self,
        fmt: str = DEFAULT_LOG_FORMAT,
        datefmt: str | None = None,
        *,
        secrets: Iterable[str] = (),
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._secrets = _normalize_secrets(secrets)

    def set_secrets(self, secrets: Iterable[str]) -> None:
        self._secrets = _normalize_secrets(secrets)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_text(rendered, self._secrets)


def configure_logging(
    settings: Settings | Config | Any | None = None,
    *,
    stream: Any = None,
    enable_file_handler: bool = True,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> Path | None:
    """Configure root logging, clamp noisy HTTP loggers, and attach redaction + rotation.

    Returns the log file path when ``enable_file_handler`` is True and creation succeeds.
    """
    cfg = Config if settings is None else settings
    level_name = str(getattr(cfg, "LOG_LEVEL", "INFO") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO

    secret_fn = getattr(cfg, "secret_values", None)
    secrets: tuple[str, ...] = tuple(secret_fn()) if callable(secret_fn) else ()

    redacting_filter = SecretRedactingFilter(secrets)
    formatter = SecretRedactingFormatter(DEFAULT_LOG_FORMAT, secrets=secrets)

    root = logging.getLogger()
    root.setLevel(level)

    # Close and replace existing handlers we own so repeated calls stay idempotent.
    for existing in list(root.handlers):
        if getattr(existing, "_stock_agent_managed", False):
            root.removeHandler(existing)
            try:
                existing.close()
            except Exception:  # noqa: BLE001
                pass
        else:
            # Ensure any pytest caplog/custom handler also gets secret redaction.
            if not any(isinstance(f, SecretRedactingFilter) for f in existing.filters):
                existing.addFilter(SecretRedactingFilter(secrets))
            else:
                for f in existing.filters:
                    if isinstance(f, SecretRedactingFilter):
                        f.set_secrets(secrets)

    stream_handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redacting_filter)
    stream_handler._stock_agent_managed = True  # type: ignore[attr-defined]
    root.addHandler(stream_handler)

    log_file_path: Path | None = None
    if enable_file_handler:
        data_dir = Path(getattr(cfg, "DATA_DIR", Path("data/stock_agent")))
        log_dir = data_dir / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file_path = log_dir / "stock_agent.log"
            file_handler = RotatingFileHandler(
                log_file_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redacting_filter)
            file_handler._stock_agent_managed = True  # type: ignore[attr-defined]
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("Could not initialize rotating file log at %s: %s", log_dir, exc)
            log_file_path = None

    # Silence httpx INFO request-URL lines and httpcore DEBUG frames.
    for noisy_logger_name in ("httpx", "httpcore"):
        noisy = logging.getLogger(noisy_logger_name)
        noisy.setLevel(logging.WARNING)
        if not any(isinstance(f, SecretRedactingFilter) for f in noisy.filters):
            noisy.addFilter(SecretRedactingFilter(secrets))

    if not any(isinstance(f, SecretRedactingFilter) for f in root.filters):
        root.addFilter(SecretRedactingFilter(secrets))
    else:
        for f in root.filters:
            if isinstance(f, SecretRedactingFilter):
                f.set_secrets(secrets)

    return log_file_path


__all__ = [
    "DEFAULT_LOG_BACKUP_COUNT",
    "DEFAULT_LOG_FORMAT",
    "DEFAULT_LOG_MAX_BYTES",
    "SecretRedactingFilter",
    "SecretRedactingFormatter",
    "configure_logging",
    "redact_text",
]
