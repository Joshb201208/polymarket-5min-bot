"""
Rich Telegram alert formatting for all agents.

Sends alerts via the Telegram Bot API (httpx POST).
"""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from agents.common.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured — skipping alert")
        return False
    try:
        resp = httpx.post(
            f"{_TELEGRAM_API}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return True
        logger.error("Telegram API %s: %s", resp.status_code, resp.text[:300])
        return False
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


# ── Alert builders ────────────────────────────────────────────

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
    """Send a NEW_OPPORTUNITY alert with full reasoning."""
    edge_pct = abs(edge) * 100
    conf_emoji = {"HIGH": "\u26a1", "MEDIUM": "\u2705", "LOW": "\u26a0\ufe0f"}.get(
        confidence.upper(), "\u2753"
    )
    reasoning_lines = "\n".join(f"\u2022 {r}" for r in reasoning)
    sources_str = ", ".join(sources) if sources else "Market data"

    text = (
        f"\U0001f3af <b>EDGE FOUND — {agent_name}</b>\n"
        f"\n"
        f"\U0001f4ca <b>Market:</b> {market_title}\n"
        f"\U0001f517 {market_url}\n"
        f"\n"
        f"\U0001f4b0 Market Price: YES @ {market_price * 100:.0f}\u00a2\n"
        f"\U0001f4d0 Our Fair Value: YES @ {fair_value * 100:.0f}\u00a2\n"
        f"\U0001f4c8 Edge: {'+' if edge > 0 else '-'}{edge_pct:.1f}% ({confidence.upper()} CONFIDENCE)\n"
        f"\n"
        f"\U0001f3e6 Recommended: <b>BUY {direction.upper()}</b>\n"
        f"\U0001f4b5 Suggested Size: ${suggested_size:.0f} (paper)\n"
        f"\u23f0 Resolves: {resolves}\n"
        f"\n"
        f"\U0001f4dd <b>Reasoning:</b>\n{reasoning_lines}\n"
        f"\n"
        f"\U0001f4f0 Sources: {sources_str}\n"
        f"{conf_emoji} Confidence: {confidence.upper()}"
    )
    return _send_message(text)


def send_daily_summary(
    agent_stats: list[dict[str, Any]],
    paper_stats: dict[str, Any],
    active_positions: int,
) -> bool:
    """Send a daily summary of all agents' performance."""
    lines = ["\U0001f4ca <b>DAILY SUMMARY — All Agents</b>\n"]
    for stat in agent_stats:
        lines.append(
            f"{stat['name']}: {stat['alerts']} alerts sent, "
            f"{stat['scanned']} markets scanned"
        )
    lines.append("")
    lines.append("<b>Paper Trading Performance:</b>")
    lines.append(f"\u2022 Total Bets: {paper_stats.get('total', 0)} "
                 f"({paper_stats.get('resolved', 0)} resolved)")
    lines.append(f"\u2022 Win Rate: {paper_stats.get('win_rate', 0):.1f}% "
                 f"({paper_stats.get('wins', 0)}/{paper_stats.get('resolved', 0)})")
    lines.append(f"\u2022 Paper P&L: ${paper_stats.get('pnl', 0):+.2f}")
    lines.append(f"\u2022 Avg Edge: {paper_stats.get('avg_edge', 0):.1f}%")
    if paper_stats.get("best_bet"):
        lines.append(f"\u2022 Best Bet: {paper_stats['best_bet']}")
    if paper_stats.get("worst_bet"):
        lines.append(f"\u2022 Worst Bet: {paper_stats['worst_bet']}")
    lines.append(f"\nActive Positions: {active_positions} pending resolution")

    return _send_message("\n".join(lines))


def send_startup_alert() -> bool:
    """Notify that the agents service has started."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = (
        "\U0001f680 <b>Polymarket AI Agents — Online</b>\n"
        f"\n"
        f"Started at: {now}\n"
        f"Agents: Events, Soccer, NBA\n"
        f"Mode: Paper trading (advisory only)"
    )
    return _send_message(text)


def send_market_resolved(
    agent_name: str,
    market_title: str,
    direction: str,
    entry_price: float,
    outcome: str,
    pnl: float,
) -> bool:
    """Notify that a tracked market has resolved."""
    emoji = "\U0001f389" if pnl > 0 else "\U0001f4c9"
    text = (
        f"{emoji} <b>MARKET RESOLVED — {agent_name}</b>\n"
        f"\n"
        f"\U0001f4ca {market_title}\n"
        f"Direction: {direction} @ {entry_price * 100:.0f}\u00a2\n"
        f"Outcome: {outcome}\n"
        f"Paper P&L: <b>${pnl:+.2f}</b>"
    )
    return _send_message(text)


def send_error_alert(agent_name: str, error: str) -> bool:
    """Alert on critical errors."""
    text = (
        f"\u26a0\ufe0f <b>AGENT ERROR — {agent_name}</b>\n"
        f"\n{error[:500]}"
    )
    return _send_message(text)
