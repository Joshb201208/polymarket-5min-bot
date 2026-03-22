"""Rich Telegram alert formatting — sends structured messages via Bot API."""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from agents.common.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_http = httpx.Client(timeout=15)
_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _send(text: str, parse_mode: str = "HTML") -> bool:
    """Low-level send via Telegram Bot API. Returns True on success."""
    try:
        resp = _http.post(
            f"{_API_URL}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
        )
        if resp.status_code == 200:
            return True
        logger.warning("Telegram send failed (%s): %s", resp.status_code, resp.text[:200])
        return False
    except Exception:
        logger.exception("Telegram send error")
        return False


# ── Alert types ──────────────────────────────────────────────

def send_edge_alert(
    agent_name: str,
    market_title: str,
    market_url: str,
    market_price: float,
    fair_value: float,
    edge: float,
    confidence: str,
    direction: str,
    suggested_size: float,
    resolves: str,
    reasoning: list[str],
    sources: list[str],
) -> bool:
    """Send a NEW_OPPORTUNITY edge-found alert."""
    edge_pct = abs(edge) * 100
    reasoning_lines = "\n".join(f"• {r}" for r in reasoning)
    sources_text = ", ".join(sources) if sources else "Market data"

    text = (
        f"🎯 <b>EDGE FOUND — {agent_name}</b>\n\n"
        f"📊 <b>Market:</b> {market_title}\n"
        f"🔗 {market_url}\n\n"
        f"💰 Market Price: YES @ {market_price:.0f}¢\n"
        f"📐 Our Fair Value: YES @ {fair_value:.0f}¢\n"
        f"📈 Edge: +{edge_pct:.1f}% ({confidence} CONFIDENCE)\n\n"
        f"🏦 Recommended: {direction}\n"
        f"💵 Suggested Size: ${suggested_size:.0f} (paper)\n"
        f"⏰ Resolves: {resolves}\n\n"
        f"📝 <b>Reasoning:</b>\n{reasoning_lines}\n\n"
        f"📰 Sources: {sources_text}\n"
        f"⚡ Confidence: {confidence}"
    )
    return _send(text)


def send_market_resolved(
    agent_name: str,
    market_title: str,
    outcome: str,
    pnl: float,
    entry_price: float,
) -> bool:
    """Send a MARKET_RESOLVED notification."""
    emoji = "✅" if pnl >= 0 else "❌"
    text = (
        f"{emoji} <b>RESOLVED — {agent_name}</b>\n\n"
        f"📊 {market_title}\n"
        f"Result: <b>{outcome}</b>\n"
        f"Entry: {entry_price:.0f}¢ → P&L: {'+' if pnl >= 0 else ''}{pnl:.2f}\n"
    )
    return _send(text)


def send_daily_summary(stats: dict[str, Any]) -> bool:
    """Send the DAILY_SUMMARY report."""
    agent_lines = ""
    for name, info in stats.get("agents", {}).items():
        agent_lines += (
            f"  {name}: {info.get('alerts_sent', 0)} alerts sent, "
            f"{info.get('markets_scanned', 0)} markets scanned\n"
        )

    paper = stats.get("paper", {})
    total_bets = paper.get("total_bets", 0)
    resolved = paper.get("resolved", 0)
    win_rate = paper.get("win_rate", 0)
    wins = paper.get("wins", 0)
    pnl = paper.get("pnl", 0.0)
    avg_edge = paper.get("avg_edge", 0.0)
    best = paper.get("best_bet", "N/A")
    worst = paper.get("worst_bet", "N/A")
    active = paper.get("active_positions", 0)

    text = (
        f"📊 <b>DAILY SUMMARY — All Agents</b>\n\n"
        f"{agent_lines}\n"
        f"<b>Paper Trading Performance:</b>\n"
        f"• Total Bets: {total_bets} ({resolved} resolved)\n"
        f"• Win Rate: {win_rate:.1f}% ({wins}/{resolved})\n"
        f"• Paper P&L: {'+' if pnl >= 0 else ''}${pnl:.2f}\n"
        f"• Avg Edge: {avg_edge:.1f}%\n"
        f"• Best Bet: {best}\n"
        f"• Worst Bet: {worst}\n\n"
        f"Active Positions: {active} pending resolution"
    )
    return _send(text)


def send_startup_alert() -> bool:
    """Send a startup notification when the agents come online."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = (
        f"🚀 <b>Polymarket AI Agents Online</b>\n\n"
        f"⏰ Started: {now}\n"
        f"📡 Agent 1: Events scanner (every 2h)\n"
        f"⚽ Agent 2: Soccer scanner (every 1h)\n"
        f"🏀 Agent 3: NBA scanner (every 1h)\n\n"
        f"Mode: Paper trading only"
    )
    return _send(text)


def send_error_alert(agent_name: str, error: str) -> bool:
    """Send an error notification (throttled externally if needed)."""
    text = (
        f"⚠️ <b>ERROR — {agent_name}</b>\n\n"
        f"{error[:500]}"
    )
    return _send(text)
