"""Sharp money / whale detector — flags large price movements between scan cycles.

Compares market prices between consecutive scans. If any outcome moves >5%
in one cycle, flags it as a "sharp movement." Stores data in
data/sharp_movements.json and sends Telegram whale alerts.

Keeps last 30 days of data, auto-prunes older entries.
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
_SHARP_MOVEMENTS_PATH = _DATA_DIR / "sharp_movements.json"
_LAST_PRICES_PATH = _DATA_DIR / "whale_last_prices.json"

# Movement threshold — flag if any outcome price changes by >5% in one cycle
_SHARP_THRESHOLD = 0.05


def _load_movements() -> dict[str, Any]:
    if not _SHARP_MOVEMENTS_PATH.exists():
        return {"movements": []}
    try:
        data = json.loads(_SHARP_MOVEMENTS_PATH.read_text())
        if "movements" not in data:
            data["movements"] = []
        return data
    except (json.JSONDecodeError, OSError):
        return {"movements": []}


def _save_movements(data: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SHARP_MOVEMENTS_PATH.write_text(json.dumps(data, indent=2, default=str))


def _load_last_prices() -> dict[str, Any]:
    if not _LAST_PRICES_PATH.exists():
        return {}
    try:
        return json.loads(_LAST_PRICES_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_last_prices(data: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LAST_PRICES_PATH.write_text(json.dumps(data, indent=2, default=str))


def check_movements(
    markets: list[dict],
    open_position_token_ids: set[str] | None = None,
    sport: str = "nba",
) -> list[dict]:
    """Check all markets for sharp price movements since last scan.

    Each market dict should have:
        - id: market ID
        - question: market text
        - outcome_prices: list[float] (e.g. [0.10, 0.90])
        - clob_token_ids: list[str]

    Returns list of sharp movement dicts (also saved to disk).
    """
    last_prices = _load_last_prices()
    movements_data = _load_movements()
    new_movements = []
    now = datetime.now(timezone.utc).isoformat()

    position_tokens = open_position_token_ids or set()

    for market in markets:
        market_id = market.get("id", "")
        question = market.get("question", "")
        current_prices = market.get("outcome_prices", [])
        token_ids = market.get("clob_token_ids", [])

        if not current_prices or not market_id:
            continue

        old_entry = last_prices.get(market_id)

        if old_entry:
            old_prices = old_entry.get("prices", [])
            # Compare each outcome
            for i, (new_p, old_p) in enumerate(zip(current_prices, old_prices)):
                delta = abs(new_p - old_p)
                if delta >= _SHARP_THRESHOLD:
                    # Determine direction
                    if len(current_prices) >= 2:
                        if new_p > old_p and i == 0:
                            direction = "underdog_up"
                        elif new_p < old_p and i == 0:
                            direction = "favorite_up"
                        else:
                            direction = "movement"
                    else:
                        direction = "movement"

                    # Check if we have a position
                    our_position = "none"
                    for tid in token_ids:
                        if tid in position_tokens:
                            # Check if movement is aligned with or against our position
                            our_position = "aligned" if new_p > old_p else "against"
                            break

                    movement = {
                        "market_id": market_id,
                        "market_question": question,
                        "sport": sport,
                        "time": now,
                        "old_price": old_prices,
                        "new_price": list(current_prices),
                        "delta": round(delta, 4),
                        "direction": direction,
                        "our_position": our_position,
                    }
                    new_movements.append(movement)
                    movements_data["movements"].append(movement)

                    logger.info(
                        "SHARP MOVEMENT: %s | %.1f¢ → %.1f¢ (Δ%.1f¢) | %s",
                        question, old_p * 100, new_p * 100, delta * 100, direction,
                    )
                    break  # Only flag once per market per cycle

        # Update last prices for next cycle
        last_prices[market_id] = {
            "prices": list(current_prices),
            "time": now,
        }

    _save_last_prices(last_prices)

    if new_movements:
        _save_movements(movements_data)

    return new_movements


def prune_old_data() -> int:
    """Remove sharp movements older than 30 days. Returns count removed."""
    data = _load_movements()
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    original_count = len(data["movements"])

    data["movements"] = [
        m for m in data["movements"]
        if _parse_time(m.get("time", "")) >= cutoff
    ]

    removed = original_count - len(data["movements"])
    if removed > 0:
        _save_movements(data)

    return removed


def _parse_time(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def get_recent_movements(days: int = 7) -> list[dict]:
    """Return sharp movements from the last N days."""
    data = _load_movements()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [
        m for m in data["movements"]
        if _parse_time(m.get("time", "")) >= cutoff
    ]


def get_movement_count(days: int = 1) -> int:
    """Return count of sharp movements in the last N days."""
    return len(get_recent_movements(days))


async def send_whale_alert(movement: dict, config: SharedConfig | None = None) -> bool:
    """Send a Telegram whale alert."""
    import httpx

    cfg = config or SharedConfig()
    if not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        return False

    q = movement["market_question"]
    old_prices = movement.get("old_price", [])
    new_prices = movement.get("new_price", [])
    delta = movement.get("delta", 0)
    our_pos = movement.get("our_position", "none")

    # Format old/new prices
    old_str = "/".join(f"{p*100:.1f}¢" for p in old_prices)
    new_str = "/".join(f"{p*100:.1f}¢" for p in new_prices)

    if our_pos == "aligned":
        text = (
            f"🐋 <b>WHALE ALIGNED</b>: {q}\n"
            f"{old_str} → {new_str} (Δ{delta*100:.1f}¢)\n"
            f"Sharp money confirming our position"
        )
    elif our_pos == "against":
        text = (
            f"🐋 <b>WHALE AGAINST</b>: {q}\n"
            f"{old_str} → {new_str} (Δ{delta*100:.1f}¢)\n"
            f"Sharp money moving against us — watch closely"
        )
    else:
        text = (
            f"🐋 <b>WHALE ALERT</b>: {q}\n"
            f"{old_str} → {new_str} (Δ{delta*100:.1f}¢)\n"
            f"Sharp money detected"
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
        logger.error("Whale alert send error: %s", e)
        return False
