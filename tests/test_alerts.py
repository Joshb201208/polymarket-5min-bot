"""Tests for AlertService, Discord/Telegram hardening, and HeartbeatClient."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from stock_agent.alerts import (
    AlertService,
    redact_secrets_in_text,
)
from stock_agent.config import Config
from stock_agent.discord_alerts import (
    MAX_EMBED_FIELDS,
    MAX_EMBED_TOTAL_CHARS,
    MAX_FIELD_VALUE,
    DiscordReporter,
    chunk_embeds_for_discord,
    embed_char_count,
)
from stock_agent.heartbeat import HeartbeatClient
from stock_agent.telegram_alerts import TelegramReporter
from tests.fakes.world import FakeWorld


@pytest.fixture
def world(monkeypatch, tmp_path: Path) -> FakeWorld:
    w = FakeWorld()
    w.install(monkeypatch, data_dir=tmp_path)
    return w


def test_dedup_cooldown_escalation_remind_and_resolve(world: FakeWorld, tmp_path: Path) -> None:
    cfg = Config()
    cfg.DATA_DIR = tmp_path
    svc = AlertService(cfg, clock=world.clock.now)

    assert svc.alert("warning", "Provider slow", "Finnhub timed out", key="input:finnhub:news", dedupe_window_sec=600)
    assert not svc.alert("warning", "Provider slow", "Finnhub timed out", key="input:finnhub:news", dedupe_window_sec=600)
    assert svc.active_keys["input:finnhub:news"].suppressed_count == 1
    assert len(svc.outbox) == 1

    # Escalating to CRITICAL bypasses the warning cooldown immediately
    assert svc.alert("critical", "Provider down", "Required feed unreachable", key="input:finnhub:news", dedupe_window_sec=3600)
    assert len(svc.outbox) == 2
    assert svc.outbox[1].event.suppressed_before_this == 1

    # Repeat critical within 1h suppressed, then re-notifies after 1h
    assert not svc.alert("critical", "Provider down", "Still down", key="input:finnhub:news", dedupe_window_sec=3600)
    world.clock.advance(seconds=3605)
    assert svc.alert("critical", "Provider down", "Still down", key="input:finnhub:news", dedupe_window_sec=3600)
    assert len(svc.outbox) == 3

    # Resolve sends once, second resolve is a no-op
    assert svc.alert("info", "Finnhub recovered", "Back online", key="input:finnhub:news", resolved=True)
    assert not svc.alert("info", "Finnhub recovered", "Back online", key="input:finnhub:news", resolved=True)
    assert "input:finnhub:news" not in svc.active_keys


def test_outbox_persists_across_restart_and_drains_on_recovery(world: FakeWorld, tmp_path: Path) -> None:
    async def _run() -> None:
        cfg = Config()
        cfg.DATA_DIR = tmp_path
        cfg.TELEGRAM_BOT_TOKEN = ""
        cfg.TELEGRAM_CHAT_ID = ""

        world.discord.fail("/api/v10/channels/*/messages", "5xx", method="POST", times=1)
        svc1 = AlertService(cfg, clock=world.clock.now)
        svc1.alert_order_rejected("NVDA", "buy", 10, "Insufficient buying power")

        delivered = await svc1.flush()
        assert delivered == 0
        assert len(svc1.outbox) == 1

        # Simulate process restart; advance past backoff and flush cleanly
        world.clock.advance(seconds=180)
        svc2 = AlertService(cfg, clock=world.clock.now)
        assert len(svc2.outbox) == 1
        assert "order_rejected:NVDA:buy:stock" in svc2.active_keys

        delivered2 = await svc2.flush()
        assert delivered2 == 1
        assert len(svc2.outbox) == 0
        texts = world.discord.texts()
        assert any("Order rejected for NVDA" in t and "Delayed delivery" in t for t in texts)

    asyncio.run(_run())


def test_outbox_cap_evicts_info_never_critical(world: FakeWorld, tmp_path: Path) -> None:
    cfg = Config()
    cfg.DATA_DIR = tmp_path
    cfg.ALERT_OUTBOX_MAX_ITEMS = 3
    svc = AlertService(cfg, clock=world.clock.now)

    svc.alert("critical", "Crit 1", "must keep")
    svc.alert("info", "Info 1", "drop first")
    svc.alert("warning", "Warn 1", "drop second")
    svc.alert("critical", "Crit 2", "must keep too")

    titles = [it.event.title for it in svc.outbox]
    assert titles == ["Crit 1", "Warn 1", "Crit 2"]


def test_429_defers_in_outbox_and_multi_embed_never_falsely_returns_true(world: FakeWorld, tmp_path: Path) -> None:
    async def _run() -> None:
        cfg = Config()
        cfg.DATA_DIR = tmp_path
        cfg.TELEGRAM_BOT_TOKEN = ""
        cfg.TELEGRAM_CHAT_ID = ""

        # 1) Direct _send_multi_embed with 4 consecutive 429s must return False
        world.discord.rate_limit(times=4, retry_after=0.5)
        disc = DiscordReporter(cfg)
        ok = await disc._send_multi_embed(cfg.DISCORD_CHANNELS["risk"], [{"title": "Test", "description": "Body"}])
        assert ok is False

        # 2) Long 429 retry_after in AlertService defers to outbox immediately without stalling
        world.discord.rate_limit(times=1, retry_after=45.0)
        svc = AlertService(cfg, discord=disc, clock=world.clock.now)
        before = world.clock.now()
        svc.alert("warning", "Rate limit test", "Should defer")
        sent = await svc.flush()
        after = world.clock.now()
        assert sent == 0
        assert (after - before).total_seconds() < 1.0
        assert len(svc.outbox) == 1

        world.clock.advance(seconds=50)
        assert await svc.flush() == 1
        assert len(svc.outbox) == 0

    asyncio.run(_run())


def test_critical_mentions_owner_or_here_and_warning_blocks_mentions(world: FakeWorld, tmp_path: Path) -> None:
    async def _run() -> None:
        cfg = Config()
        cfg.DATA_DIR = tmp_path
        cfg.TELEGRAM_BOT_TOKEN = ""
        cfg.TELEGRAM_CHAT_ID = ""

        svc = AlertService(cfg, clock=world.clock.now)
        svc.alert_missing_stop("JPM", 50, "stop order rejected")
        svc.alert_skipped_cycle("weekly_analysis", "LLM budget exhausted")
        await svc.flush()

        assert len(world.discord.messages) == 2
        crit_msg, warn_msg = world.discord.messages[0], world.discord.messages[1]
        assert "@here" in (crit_msg.get("content") or "")
        assert crit_msg["allowed_mentions"] == {"parse": ["everyone"]}
        assert warn_msg.get("content") is None
        assert warn_msg["allowed_mentions"] == {"parse": []}

        cfg.DISCORD_OWNER_USER_ID = "9876543210"
        svc.alert_order_rejected("AAPL", "sell", 20, "market closed")
        await svc.flush()
        user_msg = world.discord.messages[-1]
        assert "<@9876543210>" in (user_msg.get("content") or "")
        assert user_msg["allowed_mentions"] == {"parse": [], "users": ["9876543210"]}

    asyncio.run(_run())


def test_discord_sanitizer_clamps_fields_empty_strings_and_6000_char_batches() -> None:
    huge_embed = {
        "title": "T" * 400,
        "description": "",
        "fields": [
            {"name": "" if i == 0 else f"Field {i}", "value": "" if i == 1 else ("X" * 1500), "inline": True}
            for i in range(32)
        ],
    }
    batches = chunk_embeds_for_discord([huge_embed, huge_embed], mode="PAPER")
    assert len(batches) >= 2
    for batch in batches:
        assert len(batch) <= 10
        assert sum(embed_char_count(e) for e in batch) <= MAX_EMBED_TOTAL_CHARS
        for emb in batch:
            assert len(emb.get("fields", [])) <= MAX_EMBED_FIELDS
            for f in emb.get("fields", []):
                assert 1 <= len(f["name"]) <= 256
                assert 1 <= len(f["value"]) <= MAX_FIELD_VALUE


def test_domain_messages_plain_english_and_secret_redaction(world: FakeWorld, tmp_path: Path) -> None:
    async def _run() -> None:
        cfg = Config()
        cfg.DATA_DIR = tmp_path
        svc = AlertService(cfg, clock=world.clock.now)

        @dataclass
        class FakeDiff:
            kind: str
            symbol: str
            severity: str
            ledger: object
            broker: object
            explanation: str = ""
            action: str = ""

        svc.alert_reconcile_diffs([FakeDiff("short_stock", "PDD", "critical", 0, -85, "Double sell prior to upgrade")])
        svc.alert_input_health("FMP", "quotes", "unhealthy", reason="GET https://x?apikey=SUPERSECRET123 402", required_for_trading=False)
        svc.alert_deploy_event("startup_failed", "abcdef1234567890", reason="Health check timed out", rollback=True)
        svc.send_news_reaction(
            symbol="NVDA",
            headline="Nvidia unveils next-gen datacenter rack schedule",
            source="Alpaca News",
            published_at=datetime(2026, 11, 23, 15, 0),
            decision="BUY",
            reason="Strong demand signal with fresh quote and room under the 15% position cap.",
            conviction=9,
        )
        svc.send_option_structure_opened(
            underlying="QQQ",
            strategy="bear_put_spread",
            legs_summary="Buy 1x QQQ 640P / Sell 1x QQQ 610P exp 2026-12-18",
            debit_total=950.0,
            max_loss=950.0,
            max_profit=2050.0,
            conviction=8,
            origin="NEWS",
            reason="Downside hedge on AI pullback.",
        )
        svc.send_option_structure_closed(
            underlying="QQQ",
            strategy="bear_put_spread",
            legs_summary="1x QQQ 640P/610P",
            realized_pnl=480.0,
            exit_reason="Hit 50% profit target.",
            held_days=4,
        )
        svc.send_normalise_plan(["Turn on no-shorting at the broker", "Cover accidental short positions"])
        svc.send_normalise_result(["Covered short positions and verified stops on all holdings"], all_clean=True)

        await svc.flush(max_items=20)
        combined = "\n".join(world.discord.texts())
        assert "SUPERSECRET123" not in combined
        assert "[REDACTED]" in combined
        assert "Unexpected short position in PDD" in combined
        assert "Update rolled back to abcdef123456" in combined
        assert "Nvidia unveils next-gen datacenter rack schedule" in combined
        assert "Max Possible Loss" in combined and "$950" in combined
        assert "Hit 50% profit target" in combined
        assert "Account Clean-Up Plan" in combined
        assert "Account Clean-Up Complete" in combined

    asyncio.run(_run())


def test_telegram_escapes_html_and_redacts_token() -> None:
    assert "apikey=[REDACTED]" in redact_secrets_in_text("https://a.com?apikey=abc123&x=1")
    assert "/bot[REDACTED]" in redact_secrets_in_text("https://api.telegram.org/bot12345:AAbbCC_dd/sendMessage")

    cfg = Config()
    cfg.TELEGRAM_BOT_TOKEN = "99999:SECRET_TG_TOK"
    cfg.TELEGRAM_CHAT_ID = "1"
    tg = TelegramReporter(cfg)
    assert "SECRET_TG_TOK" not in tg._redact("Error calling https://api.telegram.org/bot99999:SECRET_TG_TOK/sendMessage")


def test_heartbeat_noop_when_unset_and_daily_discord_line_by_default(world: FakeWorld, tmp_path: Path) -> None:
    async def _run() -> None:
        cfg = Config()
        cfg.DATA_DIR = tmp_path
        cfg.HEARTBEAT_PING_URL = ""
        svc = AlertService(cfg, clock=world.clock.now)
        hb = HeartbeatClient(cfg, clock=world.clock.now)

        assert hb.enabled is False
        assert await hb.maybe_ping() is False

        assert await hb.tick(svc, ok=True, note="weekend idle") is True
        assert await hb.tick(svc, ok=True, note="weekend idle") is False
        await svc.flush()
        assert any("Daily Heartbeat" in t for t in world.discord.texts())

    asyncio.run(_run())
