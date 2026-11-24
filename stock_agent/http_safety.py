"""Shared HTTP transport, header-only provider auth, and log-redaction safeguards.

Guarantees:
- API keys, tokens, and secrets are passed in HTTP headers (`APCA-API-KEY-ID`,
  `X-Finnhub-Token`, `apikey`, `User-Agent`) selected strictly by verified parsed
  HTTPS hostname — never by URL substring and never in URL query strings.
- `httpx` and `httpcore` INFO-level request URL logs are suppressed to WARNING.
- `RedactingLogFilter` scrubs query-string credentials, header credentials (including
  `Bearer`, `Bot`, `Basic`, `APCA-API-KEY-ID`, `APCA-API-SECRET-KEY`, `X-Finnhub-Token`),
  and exception tracebacks (`record.exc_info` / `record.exc_text`) before formatting.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any
from urllib.parse import urlsplit

import httpx

from stock_agent.config import Config

_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:apikey|api_key|token|access_token|secret|password|auth|key)=)([^&\s'\"<>]+)"
)
_AUTH_SCHEME_RE = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|bot|basic|token)\s+)([^\s,'\"<>};]+)"
)
_KV_SECRET_RE = re.compile(
    r"(?i)(\b(?:apikey|api_key|x-finnhub-token|apca-api-key-id|apca-api-secret-key|"
    r"authorization|discord_bot_token|telegram_bot_token|bot_token|secret|password)\s*[:=]\s*)"
    r"([^\s,'\"<>};]+)"
)


def scrub_secrets(text: str) -> str:
    """Redact query-string and header-style credentials from arbitrary text."""
    if not text:
        return text
    cleaned = _QUERY_SECRET_RE.sub(r"\1REDACTED", text)
    cleaned = _AUTH_SCHEME_RE.sub(r"\1REDACTED", cleaned)
    cleaned = _KV_SECRET_RE.sub(r"\1REDACTED", cleaned)
    return cleaned


def _scrub_arg(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, tuple):
        return tuple(_scrub_arg(item) for item in value)
    if isinstance(value, list):
        return [_scrub_arg(item) for item in value]
    if isinstance(value, dict):
        return {k: _scrub_arg(v) for k, v in value.items()}
    if type(value).__name__ in {"URL", "Request"}:
        return scrub_secrets(str(value))
    return value


class RedactingLogFilter(logging.Filter):
    """Logging filter that scrubs secrets from messages, args, and exception tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub_secrets(record.msg)
        elif record.msg is not None:
            record.msg = scrub_secrets(str(record.msg))

        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub_arg(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_scrub_arg(a) for a in record.args)
            else:
                record.args = (_scrub_arg(record.args),)

        if record.exc_info:
            formatted = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            record.exc_text = scrub_secrets(formatted)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = scrub_secrets(record.exc_text)

        if record.stack_info:
            record.stack_info = scrub_secrets(record.stack_info)

        return True


def install_redacting_filter(target: logging.Logger | logging.Handler) -> None:
    """Attach RedactingLogFilter idempotently to a logger or handler."""
    if any(isinstance(existing, RedactingLogFilter) for existing in target.filters):
        return
    target.addFilter(RedactingLogFilter())


def configure_safe_http_logging() -> None:
    """Silence verbose httpx/httpcore URL logging and attach redacting filters."""
    for name in ("httpx", "httpcore"):
        ext_logger = logging.getLogger(name)
        if ext_logger.level == logging.NOTSET or ext_logger.level < logging.WARNING:
            ext_logger.setLevel(logging.WARNING)
        install_redacting_filter(ext_logger)

    root = logging.getLogger()
    install_redacting_filter(root)
    for handler in root.handlers:
        install_redacting_filter(handler)
    install_redacting_filter(logging.getLogger("stock_agent"))


def alpaca_headers(config: Any = Config) -> dict[str, str]:
    """Build Alpaca Market Data / Trading authentication headers."""
    key = getattr(config, "ALPACA_API_KEY", "") or ""
    secret = getattr(config, "ALPACA_SECRET_KEY", "") or ""
    if not key or not secret:
        return {}
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def finnhub_headers(config: Any = Config) -> dict[str, str]:
    """Build Finnhub authentication headers (never pass token in URL query string)."""
    key = getattr(config, "FINNHUB_API_KEY", "") or ""
    if not key:
        return {}
    return {"X-Finnhub-Token": key}


def fmp_headers(config: Any = Config) -> dict[str, str]:
    """Build Financial Modeling Prep header authentication (never pass apikey in URL)."""
    key = getattr(config, "FMP_API_KEY", "") or ""
    if not key:
        return {}
    return {"apikey": key}


def sec_headers(config: Any = Config) -> dict[str, str]:
    """Build SEC EDGAR fair-access User-Agent header."""
    ua = (
        getattr(config, "SEC_USER_AGENT", "")
        or "StockTradingAgent/2.0 (contact@stock-agent.invalid)"
    )
    return {
        "User-Agent": ua,
        "Accept": "application/json",
    }


def _host_matches(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


class DataHttpClient:
    """Shared async HTTP transport for market-data, news, fundamentals, and macro services."""

    def __init__(
        self,
        config: Any = Config,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 12.0,
    ) -> None:
        configure_safe_http_logging()
        self.config = config
        self.timeout = timeout
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None

    def _raw(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None or self._owned_client.is_closed:
            self._owned_client = httpx.AsyncClient(timeout=self.timeout)
        return self._owned_client

    def default_headers_for_url(self, url: str) -> dict[str, str]:
        """Select auth headers strictly from verified parsed HTTPS hostname."""
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https":
            return {}
        host = (parsed.hostname or "").lower()
        if not host:
            return {}
        if _host_matches(host, "alpaca.markets"):
            return alpaca_headers(self.config)
        if _host_matches(host, "finnhub.io"):
            return finnhub_headers(self.config)
        if _host_matches(host, "financialmodelingprep.com"):
            return fmp_headers(self.config)
        if _host_matches(host, "sec.gov"):
            return sec_headers(self.config)
        return {}

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        merged_headers = self.default_headers_for_url(url)
        if headers:
            merged_headers.update(headers)
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if merged_headers:
            kwargs["headers"] = merged_headers
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self._raw().get(url, **kwargs)

    async def post(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        merged_headers = self.default_headers_for_url(url)
        if headers:
            merged_headers.update(headers)
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if merged_headers:
            kwargs["headers"] = merged_headers
        if json is not None:
            kwargs["json"] = json
        if data is not None:
            kwargs["data"] = data
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self._raw().post(url, **kwargs)

    async def aclose(self) -> None:
        if self._owned_client is not None and not self._owned_client.is_closed:
            await self._owned_client.aclose()
            self._owned_client = None
