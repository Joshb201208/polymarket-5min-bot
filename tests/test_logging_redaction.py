"""Tests for central logging configuration, secret redaction, and __main__ fail-fast."""

from __future__ import annotations

import io
import logging
from pathlib import Path

import httpx
import pytest

from stock_agent import __main__ as entrypoint
from stock_agent import tipranks_client
from stock_agent.config import LIVE_ALPACA_BASE_URL, load_settings
from stock_agent.logging_setup import configure_logging, redact_text


def test_redact_text_scrubs_query_path_headers_and_configured_secrets() -> None:
    raw = (
        "GET https://financialmodelingprep.com/stable/quote?symbol=NVDA&apikey=fmpSecretKey123 "
        "https://finnhub.io/api/v1/news?category=general&token=finnhubSecret456 "
        "POST https://api.telegram.org/bot99887766:AAH_superSecretBotToken-XYZ/sendMessage "
        "Authorization: Bearer pplx-liveSecret789 Authorization: Bot discBotSecret000 "
        "raw_secret=customLiteralKey999"
    )
    cleaned = redact_text(raw, secrets=("customLiteralKey999",))
    for leaked in (
        "fmpSecretKey123",
        "finnhubSecret456",
        "AAH_superSecretBotToken-XYZ",
        "pplx-liveSecret789",
        "discBotSecret000",
        "customLiteralKey999",
    ):
        assert leaked not in cleaned
    assert "apikey=[REDACTED]" in cleaned
    assert "token=[REDACTED]" in cleaned
    assert "/bot99887766:[REDACTED]/sendMessage" in cleaned


def test_logging_clamps_httpx_and_redacts_query_path_and_exception_secrets(
    tmp_path: Path,
) -> None:
    settings = load_settings({
        "STOCK_DATA_DIR": str(tmp_path),
        "STOCK_LOG_LEVEL": "DEBUG",
        "FMP_API_KEY": "fmpLiveSecretValue",
        "FINNHUB_API_KEY": "finnhubLiveSecretValue",
        "TELEGRAM_BOT_TOKEN": "12345678:telegramSecretToken",
        "DISCORD_BOT_TOKEN": "discordSecretTokenValue",
    })
    buf = io.StringIO()
    log_path = configure_logging(settings, stream=buf, enable_file_handler=True)
    assert log_path is not None and log_path.is_file()

    try:
        assert logging.getLogger().level == logging.DEBUG
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING

        # 1. httpx INFO request line is suppressed completely.
        logging.getLogger("httpx").info(
            'HTTP Request: GET https://financialmodelingprep.com/stable/quote?apikey=fmpLiveSecretValue "HTTP/1.1 200 OK"'
        )

        # 2. httpx.HTTPStatusError logged via `%s` arg has its embedded URL scrubbed.
        req = httpx.Request(
            "GET",
            "https://finnhub.io/api/v1/company-news?symbol=AAPL&token=finnhubLiveSecretValue",
        )
        resp = httpx.Response(429, request=req)
        err = httpx.HTTPStatusError("429 Too Many Requests", request=req, response=resp)
        app_log = logging.getLogger("stock_agent.test_redaction")
        app_log.error("Finnhub request failed: %s", err)

        # 3. Exception traceback via logger.exception is scrubbed by formatter.
        try:
            raise RuntimeError(
                "Telegram send failed at https://api.telegram.org/bot12345678:telegramSecretToken/sendMessage "
                "and discordSecretTokenValue"
            )
        except RuntimeError:
            app_log.exception("Alert delivery crashed")

        # 4. DEBUG messages on stock_agent logger are emitted when STOCK_LOG_LEVEL=DEBUG.
        app_log.debug("Diagnostic check for FMP key fmpLiveSecretValue")

        for handler in logging.getLogger().handlers:
            handler.flush()

        stream_out = buf.getvalue()
        file_out = log_path.read_text(encoding="utf-8")

        for output in (stream_out, file_out):
            assert "HTTP Request: GET" not in output
            assert "Finnhub request failed" in output
            assert "Alert delivery crashed" in output
            assert "Diagnostic check for FMP key [REDACTED]" in output
            for secret in (
                "fmpLiveSecretValue",
                "finnhubLiveSecretValue",
                "telegramSecretToken",
                "discordSecretTokenValue",
            ):
                assert secret not in output
    finally:
        # Reset managed handlers cleanly after test.
        configure_logging(
            load_settings({"STOCK_DATA_DIR": str(tmp_path)}),
            enable_file_handler=False,
        )


def test_main_fails_fast_when_mode_and_broker_disagree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOCK_AGENT_MODE", "PAPER")
    monkeypatch.setenv("ALPACA_BASE_URL", LIVE_ALPACA_BASE_URL)
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    with pytest.raises(SystemExit) as exc_info:
        entrypoint.main()
    assert "ALPACA_BASE_URL" in str(exc_info.value)


def test_no_hardcoded_tipranks_or_mcp_secrets_in_tracked_code() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    assert not (repo_root / ".mcp.json").exists()
    assert tipranks_client._headers(load_settings({}))["X-APIKey"] == ""
    assert tipranks_client._headers(load_settings({}))["X-APIToken"] == ""
    assert tipranks_client._call_screener({"limit": 5}) == []
