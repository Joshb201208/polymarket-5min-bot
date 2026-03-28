"""Line movement tracker — records Polymarket price of every open position
at each scan cycle and sends Telegram alerts on significant drift.

Data stored in data/line_movements.json. Auto-prunes closed positions
older than 7 days.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.config import SharedConfig

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_LINE_MOVEMENTS_PATH = _DATA_DIR / "line_movements.json"

# Alert thresholds (absolute price movement)
_AGAINST_THRESHOLD = 0.05  # 5% against us
_FAVOR_THRESHOLD = 0.10    # 10% in our favor


def _load() -> dict[str, Any]:
    if not _LINE_MOVEMENTS_PATH.exists():
        return {}
    try:
        return json.loads(_LINE_MOVEMENTS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LINE_MOVEMENTS_PATH.write_text(json.dumps(data, indent=2, default=str))


def record_snapshot(
    position_id: str,
    token_id: str,
    entry_price: float,
    current_price: float,
    entry_time: str,
    game_start_time: str | None,
    sport: str,
    market_question: str,
) -> dict | None:
    """Record a price snapshot for an open position.

    Returns an alert dict if a threshold was crossed for the first time,
    otherwise None.
    """
    now = datetime.now(timezone.utc).isoformat()
    data = _load()

    if position_id not in data:
        data[position_id] = {
            "position_id": position_id,
            "token_id": token_id,
            "entry_price": entry_price,
            "entry_time": entry_time,
            "game_start_time": game_start_time,
            "sport": sport,
            "market_question": market_question,
            "snapshots": [],
            "alerted_against": False,
            "alerted_favor": False,
        }

    record = data[position_id]
    delta = round(current_price - entry_price, 4)
    record["snapshots"].append({
        "time": now,
        "price": round(current_price, 4),
        "delta": delta,
    })

    # Keep only last 500 snapshots per position to bound file size
    if len(record["snapshots"]) > 500:
        record["snapshots"] = record["snapshots"][-500:]

    alert = None

    # Check for drift AGAINST us (price dropped from entry)
    if delta < -_AGAINST_THRESHOLD and not record.get("alerted_against"):
        record["alerted_against"] = True
        alert = {
            "type": "against",
            "position_id": position_id,
            "market_question": market_question,
            "entry_price": entry_price,
            "current_price": current_price,
            "delta": delta,
            "sport": sport,
        }

    # Check for drift IN OUR FAVOR (price rose from entry)
    if delta > _FAVOR_THRESHOLD and not record.get("alerted_favor"):
        record["alerted_favor"] = True
        alert = {
            "type": "favor",
            "position_id": position_id,
            "market_question": market_question,
            "entry_price": entry_price,
            "current_price": current_price,
            "delta": delta,
            "sport": sport,
        }

    _save(data)
    return alert


def record_snapshots(positions: list[dict], get_price_fn=None) -> list[dict]:
    """Record snapshots for a list of open positions.

    Each position dict must have: id, token_id, entry_price, entry_time,
    game_start_time, market_question.

    Returns list of alert dicts for any threshold crossings.
    """
    alerts = []
    for pos in positions:
        current_price = pos.get("current_price")
        if current_price is None:
            continue

        alert = record_snapshot(
            position_id=pos["id"],
            token_id=pos["token_id"],
            entry_price=pos["entry_price"],
            current_price=current_price,
            entry_time=pos["entry_time"],
            game_start_time=pos.get("game_start_time"),
            sport=pos.get("sport", "nba"),
            market_question=pos.get("market_question", ""),
        )
        if alert:
            alerts.append(alert)

    return alerts


def mark_position_closed(position_id: str) -> None:
    """Mark a position as closed so it can be pruned later."""
    data = _load()
    if position_id in data:
        data[position_id]["closed_time"] = datetime.now(timezone.utc).isoformat()
        _save(data)


def prune_old_data() -> int:
    """Remove closed positions older than 7 days. Returns count removed."""
    data = _load()
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    to_remove = []

    for pid, record in data.items():
        closed_time = record.get("closed_time")
        if closed_time:
            try:
                ct = datetime.fromisoformat(closed_time.replace("Z", "+00:00"))
                if ct < cutoff:
                    to_remove.append(pid)
            except (ValueError, TypeError):
                pass

    for pid in to_remove:
        del data[pid]

    if to_remove:
        _save(data)

    return len(to_remove)


def get_open_movements() -> list[dict]:
    """Return line movement data for all tracked (non-closed) positions."""
    data = _load()
    result = []
    for record in data.values():
        if not record.get("closed_time"):
            result.append(record)
    return result


def get_position_movements(position_id: str) -> dict | None:
    """Return line movement data for a specific position."""
    data = _load()
    return data.get(position_id)


def get_summary() -> dict:
    """Return a summary of line movements for reporting."""
    data = _load()
    active = [r for r in data.values() if not r.get("closed_time")]

    toward_us = 0
    against_us = 0
    for record in active:
        snapshots = record.get("snapshots", [])
        if snapshots:
            latest_delta = snapshots[-1].get("delta", 0)
            if latest_delta > 0:
                toward_us += 1
            elif latest_delta < 0:
                against_us += 1

    return {
        "tracked_positions": len(active),
        "moving_toward_us": toward_us,
        "moving_against_us": against_us,
    }


async def send_line_alert(alert: dict, config: SharedConfig | None = None) -> bool:
    """Send a Telegram alert for a line movement threshold crossing."""
    import httpx

    cfg = config or SharedConfig()
    if not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        return False

    entry_cents = alert["entry_price"] * 100
    current_cents = alert["current_price"] * 100
    delta_cents = alert["delta"] * 100
    q = alert["market_question"]

    if alert["type"] == "against":
        text = (
            f"⚠️ <b>LINE MOVING AGAINST</b>: {q}\n"
            f"Entry {entry_cents:.1f}¢ → now {current_cents:.1f}¢ ({delta_cents:+.1f}¢)\n"
            f"Consider early exit"
        )
    else:
        text = (
            f"✅ <b>LINE CONFIRMING</b>: {q}\n"
            f"Entry {entry_cents:.1f}¢ → now {current_cents:.1f}¢ ({delta_cents:+.1f}¢)\n"
            f"Edge confirmed"
        )

    url = f"{cfg.TELEGRAM_API_BASE}/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": cfg.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            return resp.status_code == 200
    except Exception as e:
        logger.error("Line alert send error: %s", e)
        return False
