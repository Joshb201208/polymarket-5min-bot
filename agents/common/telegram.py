"""
Telegram alerts with rich formatting.
Supports: trade execution alerts, early exit, stop loss, backtest reports, position updates, daily summary.
"""

import logging
import httpx
from typing import Optional

from . import config

logger = logging.getLogger(__name__)

TIMEOUT = 10.0


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the configured Telegram chat."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.warning("Telegram not configured")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            logger.error(f"Telegram error: {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


# ── Alert Types ───────────────────────────────────────────────

def send_trade_executed(agent_name: str, question: str, side: str,
                        price: float, size: float, edge: float,
                        confidence: str, reasoning, mode: str,
                        order_id: str = "", balance: float = 0,
                        url: str = "") -> bool:
    """Alert for an executed trade (paper or live)."""
    # Format reasoning
    if isinstance(reasoning, list):
        reasoning_text = "\n".join(reasoning)
    else:
        reasoning_text = str(reasoning)[:300]

    price_cents = int(price * 100)
    msg = (
        f"<b>TRADE EXECUTED [{agent_name}]</b>\n\n"
        f"<b>{question}</b>\n"
        f"BUY {side} @ {price_cents}c | ${size:.2f}\n\n"
        f"Edge: <b>{edge:.1%}</b> | Confidence: <b>{confidence.upper()}</b>\n\n"
        f"<i>{reasoning_text[:300]}</i>\n\n"
        f"Mode: <b>{mode.upper()}</b>"
    )
    if order_id:
        msg += f" | Order: {order_id[:12]}"
    msg += f"\nBalance: ${balance:.2f} remaining"
    if url:
        msg += f"\n{url}"
    return send_message(msg)


def alert_opportunity(agent: str, market: dict, analysis: dict) -> bool:
    """Alert for a detected betting opportunity (below bet threshold)."""
    side = analysis.get("side", "YES")
    price = analysis.get("price", 0)
    fair_prob = analysis.get("fair_probability", 0)
    edge = analysis.get("edge", 0)
    confidence = analysis.get("confidence", "medium")
    reasoning = analysis.get("reasoning", "")
    url = market.get("url", "")

    if isinstance(reasoning, list):
        reasoning = "\n".join(reasoning)

    msg = (
        f"<b>EDGE DETECTED [{agent}]</b>\n\n"
        f"<b>{market.get('question', 'Unknown')}</b>\n"
        f"Side: <b>{side}</b> @ {price:.0%}\n"
        f"Fair value: {fair_prob:.0%} | Edge: <b>{edge:.1%}</b>\n"
        f"Confidence: {confidence}\n"
        f"Liquidity: ${market.get('liquidity', 0):,.0f}\n\n"
        f"<i>{str(reasoning)[:300]}</i>\n\n"
        f"{url}"
    )
    return send_message(msg)


def alert_paper_trade(action: str, market: dict, trade: dict) -> bool:
    """Alert for a paper trade execution."""
    msg = (
        f"<b>PAPER TRADE: {action}</b>\n\n"
        f"{market.get('question', 'Unknown')}\n"
        f"Side: {trade.get('side', '?')} @ {trade.get('price', 0):.2f}\n"
        f"Size: ${trade.get('size', 0):.2f}\n"
        f"Edge: {trade.get('edge', 0):.1%}\n"
    )
    return send_message(msg)


def alert_early_exit(position: dict, reason: str, pnl_pct: float,
                     confidence: str = "medium") -> bool:
    """Alert when selling a position early."""
    emoji = "+" if pnl_pct >= 0 else ""
    msg = (
        f"<b>EARLY EXIT: {reason}</b>\n\n"
        f"{position.get('question', 'Unknown')}\n"
        f"Confidence: <b>{confidence.upper()}</b>\n"
        f"Entry: {position.get('entry_price', 0):.2f} -> "
        f"Exit: {position.get('current_price', 0):.2f}\n"
        f"P&L: <b>{emoji}{pnl_pct:.1%}</b> "
        f"(${position.get('pnl_dollars', 0):+.2f})\n"
    )
    return send_message(msg)


def alert_stop_loss(position: dict, loss_pct: float) -> bool:
    """Alert when cutting a loss."""
    msg = (
        f"<b>STOP LOSS TRIGGERED</b>\n\n"
        f"{position.get('question', 'Unknown')}\n"
        f"Entry: {position.get('entry_price', 0):.2f} -> "
        f"Current: {position.get('current_price', 0):.2f}\n"
        f"Loss: <b>{loss_pct:.1%}</b> "
        f"(${position.get('pnl_dollars', 0):.2f})\n"
    )
    return send_message(msg)


def alert_position_update(position: dict, price_change_pct: float) -> bool:
    """Alert on significant position price movement."""
    emoji = "+" if price_change_pct >= 0 else ""
    msg = (
        f"<b>POSITION UPDATE</b>\n\n"
        f"{position.get('question', 'Unknown')}\n"
        f"Entry: {position.get('entry_price', 0):.2f} -> "
        f"Now: {position.get('current_price', 0):.2f} "
        f"({emoji}{price_change_pct:.1%})\n"
    )
    return send_message(msg)


def alert_backtest_report(results: dict) -> bool:
    """Send backtest results summary."""
    msg = (
        f"<b>BACKTEST REPORT</b>\n\n"
        f"Markets tested: {results.get('total_markets', 0)}\n"
        f"Trades: {results.get('total_trades', 0)}\n"
        f"Win rate: <b>{results.get('win_rate', 0):.1%}</b>\n"
        f"ROI: <b>{results.get('roi', 0):+.1%}</b>\n"
        f"Max drawdown: {results.get('max_drawdown', 0):.1%}\n"
        f"Sharpe ratio: {results.get('sharpe', 0):.2f}\n"
    )
    return send_message(msg)


def send_daily_summary(bankroll: dict, positions: list, day_pnl: float) -> bool:
    """Send daily bankroll and P&L summary."""
    open_count = sum(1 for p in positions if p.get("status") == "OPEN")
    total_pnl = bankroll.get("total_pnl", 0)
    capital = bankroll.get("capital", 0)
    exposure = bankroll.get("exposure", 0)

    emoji = "+" if day_pnl >= 0 else ""
    msg = (
        f"<b>DAILY SUMMARY</b>\n\n"
        f"<b>Bankroll: ${capital:.2f}</b>\n"
        f"Today P&L: {emoji}${day_pnl:.2f}\n"
        f"Total P&L: ${total_pnl:+.2f}\n"
        f"Open positions: {open_count}\n"
        f"Capital at risk: ${exposure:.2f}\n"
        f"Available: ${capital - exposure:.2f}\n\n"
        f"<b>BANKROLL STATUS</b>\n"
        f"Starting: ${config.STARTING_BANKROLL:.2f}\n"
        f"Current: ${capital:.2f}\n"
        f"Return: {((capital / config.STARTING_BANKROLL) - 1):.1%}\n"
    )
    return send_message(msg)


def send_startup_message(agent_name: str, mode: str = "") -> bool:
    """Send startup notification."""
    trading_mode = mode or config.TRADING_MODE
    msg = (
        f"<b>AGENT STARTED: {agent_name}</b>\n"
        f"Bankroll: ${config.STARTING_BANKROLL:.2f}\n"
        f"Mode: <b>{trading_mode.upper()}</b>\n"
    )
    return send_message(msg)
