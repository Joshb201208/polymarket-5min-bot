"""NBA Agent Dashboard — FastAPI Backend.

Reads JSON files from the agent's data directory and serves
computed stats, positions, trades, and research data.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Data directory — /root/polymarket-bot/data/ on VPS, ./data/ locally
# ---------------------------------------------------------------------------
# Try local data dir first (development), then VPS path
_local_data = Path(__file__).parent / "data"
_vps_data = Path("/root/polymarket-bot/data")
_env_data = os.environ.get("DATA_DIR")

if _env_data:
    DATA_DIR = Path(_env_data)
elif _local_data.exists():
    DATA_DIR = _local_data
else:
    try:
        if _vps_data.exists():
            DATA_DIR = _vps_data
        else:
            DATA_DIR = _local_data
    except PermissionError:
        DATA_DIR = _local_data

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="NBA Agent Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_json(filename: str) -> Any:
    """Read a JSON file from the data directory. Returns {} on failure."""
    path = DATA_DIR / filename
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp string."""
    if not ts:
        return None
    try:
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/status")
def get_status() -> dict:
    bankroll = _read_json("bankroll.json")
    positions = _read_json("positions.json").get("positions", [])
    trades = _read_json("trades.json").get("trades", [])

    # Determine mode from latest trade
    mode = "paper"
    if trades:
        mode = trades[-1].get("mode", "paper")

    # Compute uptime from earliest trade timestamp
    uptime_hours = 0.0
    if trades:
        timestamps = [_parse_ts(t.get("timestamp")) for t in trades]
        timestamps = [t for t in timestamps if t is not None]
        if timestamps:
            earliest = min(timestamps)
            uptime_hours = round(
                (datetime.now(timezone.utc) - earliest).total_seconds() / 3600, 1
            )

    # Last scan time = most recent trade timestamp
    last_scan = None
    if trades:
        timestamps_str = [t.get("timestamp") for t in trades if t.get("timestamp")]
        if timestamps_str:
            last_scan = max(timestamps_str)

    open_positions = [p for p in positions if p.get("status") == "open"]

    return {
        "bankroll": bankroll.get("current_bankroll", 0),
        "starting_bankroll": bankroll.get("starting_bankroll", 0),
        "peak_bankroll": bankroll.get("peak_bankroll", 0),
        "is_paused": bankroll.get("is_paused", False),
        "mode": mode,
        "uptime_hours": uptime_hours,
        "last_scan": last_scan,
        "open_positions_count": len(open_positions),
        "total_positions": len(positions),
        "data_sources": ["ESPN", "The Odds API", "BallDontLie", "NBA CDN"],
    }


@app.get("/api/positions")
def get_positions() -> dict:
    positions = _read_json("positions.json").get("positions", [])
    open_pos = [p for p in positions if p.get("status") == "open"]
    closed_pos = [p for p in positions if p.get("status") != "open"]
    return {"open": open_pos, "closed": closed_pos}


@app.get("/api/trades")
def get_trades() -> dict:
    trades = _read_json("trades.json").get("trades", [])
    return {"trades": trades}


@app.get("/api/stats")
def get_stats() -> dict:
    positions = _read_json("positions.json").get("positions", [])
    bankroll_data = _read_json("bankroll.json")
    starting = bankroll_data.get("starting_bankroll", 750)

    closed = [p for p in positions if p.get("status") != "open"]

    # Basic counts — determine win/loss from P&L, not status string
    total_closed = len(closed)
    wins = [p for p in closed if (p.get("pnl") or 0) > 0]
    losses = [p for p in closed if (p.get("pnl") or 0) <= 0]

    win_count = len(wins)
    loss_count = len(losses)
    win_rate = round((win_count / total_closed * 100) if total_closed > 0 else 0, 1)

    # P&L
    pnls = [p.get("pnl", 0) or 0 for p in closed]
    total_pnl = round(sum(pnls), 2)
    roi = round((total_pnl / starting * 100) if starting > 0 else 0, 1)

    # Average edge
    edges = [p.get("edge_at_entry", 0) or 0 for p in closed]
    avg_edge = round((sum(edges) / len(edges) * 100) if edges else 0, 1)

    # Win / loss averages
    win_pnls = [p.get("pnl", 0) or 0 for p in wins]
    loss_pnls = [p.get("pnl", 0) or 0 for p in losses]
    avg_win = round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0
    avg_loss = round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0

    # Profit factor
    gross_profit = sum(p for p in win_pnls if p > 0)
    gross_loss = abs(sum(p for p in loss_pnls if p < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    # Equity curve — build daily P&L
    daily_map: dict[str, float] = defaultdict(float)
    for p in closed:
        exit_ts = _parse_ts(p.get("exit_time"))
        if exit_ts:
            day = exit_ts.strftime("%Y-%m-%d")
            daily_map[day] += p.get("pnl", 0) or 0

    sorted_days = sorted(daily_map.keys())
    equity_curve = []
    running = starting
    for day in sorted_days:
        running += daily_map[day]
        equity_curve.append({
            "date": day,
            "pnl": round(daily_map[day], 2),
            "bankroll": round(running, 2),
        })

    # Max drawdown
    peak = starting
    max_dd = 0
    running = starting
    for day in sorted_days:
        running += daily_map[day]
        if running > peak:
            peak = running
        dd = (peak - running) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 1)

    # Best / worst trade
    best_trade = max(closed, key=lambda p: p.get("pnl", 0) or 0) if closed else None
    worst_trade = min(closed, key=lambda p: p.get("pnl", 0) or 0) if closed else None

    # Win rate by bet type (infer from slug)
    type_wins: dict[str, int] = defaultdict(int)
    type_total: dict[str, int] = defaultdict(int)
    for p in closed:
        slug = (p.get("market_slug") or "").lower()
        if "-spread-" in slug:
            bet_type = "spread"
        elif "-total-" in slug:
            bet_type = "total"
        elif "champion" in slug or "finals" in slug:
            bet_type = "futures"
        else:
            bet_type = "moneyline"
        type_total[bet_type] += 1
        if (p.get("pnl") or 0) > 0:
            type_wins[bet_type] += 1

    win_rate_by_type = {}
    for bt, count in type_total.items():
        win_rate_by_type[bt] = round(type_wins[bt] / count * 100, 1) if count > 0 else 0

    # Streaks
    sorted_closed = sorted(
        closed,
        key=lambda p: p.get("exit_time") or "",
    )
    current_streak = 0
    streak_type = ""
    best_streak = 0
    worst_streak = 0
    temp_streak = 0
    for p in sorted_closed:
        is_win = (p.get("pnl") or 0) > 0
        if temp_streak == 0:
            temp_streak = 1 if is_win else -1
        elif is_win and temp_streak > 0:
            temp_streak += 1
        elif not is_win and temp_streak < 0:
            temp_streak -= 1
        else:
            temp_streak = 1 if is_win else -1

        if temp_streak > 0:
            best_streak = max(best_streak, temp_streak)
        else:
            worst_streak = min(worst_streak, temp_streak)

    current_streak = abs(temp_streak)
    streak_type = "W" if temp_streak > 0 else "L"

    return {
        "total_trades": total_closed,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "roi": roi,
        "avg_edge": avg_edge,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
        "best_trade": {
            "question": best_trade.get("market_question", ""),
            "pnl": best_trade.get("pnl", 0),
        } if best_trade else None,
        "worst_trade": {
            "question": worst_trade.get("market_question", ""),
            "pnl": worst_trade.get("pnl", 0),
        } if worst_trade else None,
        "daily_pnl": equity_curve,
        "win_rate_by_type": win_rate_by_type,
        "streak": {
            "current": current_streak,
            "type": streak_type,
            "best": best_streak,
            "worst": abs(worst_streak),
        },
    }


@app.get("/api/research")
def get_research() -> dict:
    data = _read_json("research_log.json")
    research = data.get("research", [])
    # Sort by game_time descending
    research.sort(key=lambda r: r.get("game_time", ""), reverse=True)
    return {"research": research}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/api/performance/{period}")
def get_performance(period: str) -> dict:
    """Return performance stats for a given period: today, week, month, all."""
    positions = _read_json("positions.json").get("positions", [])
    bankroll_data = _read_json("bankroll.json")
    starting = bankroll_data.get("starting_bankroll", 440.58)

    closed = [p for p in positions if p.get("status") != "open"]

    # Filter by period
    now = datetime.now(timezone.utc)
    if period == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        from datetime import timedelta as td
        cutoff = now - td(days=7)
    elif period == "month":
        from datetime import timedelta as td
        cutoff = now - td(days=30)
    else:  # all
        cutoff = datetime(2020, 1, 1, tzinfo=timezone.utc)

    filtered = []
    for p in closed:
        exit_ts = _parse_ts(p.get("exit_time"))
        if exit_ts and exit_ts >= cutoff:
            filtered.append(p)

    total = len(filtered)
    wins = [p for p in filtered if (p.get("pnl") or 0) > 0]
    losses = [p for p in filtered if (p.get("pnl") or 0) <= 0]
    win_count = len(wins)
    loss_count = len(losses)

    pnls = [p.get("pnl", 0) or 0 for p in filtered]
    total_pnl = round(sum(pnls), 2)
    total_invested = sum(p.get("cost", 0) or 0 for p in filtered)
    roi = round((total_pnl / total_invested * 100) if total_invested > 0 else 0, 1)

    win_pnls = [p.get("pnl", 0) or 0 for p in wins]
    loss_pnls = [p.get("pnl", 0) or 0 for p in losses]
    avg_win = round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0
    avg_loss = round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0

    gross_profit = sum(p for p in win_pnls if p > 0)
    gross_loss = abs(sum(p for p in loss_pnls if p < 0))
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    return {
        "period": period,
        "bets": total,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": round((win_count / total * 100) if total > 0 else 0, 1),
        "pnl": total_pnl,
        "roi": roi,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": pf,
        "total_invested": round(total_invested, 2),
    }


@app.get("/api/calibration")
def get_calibration() -> dict:
    """Return self-learning calibration data."""
    cal_data = _read_json("calibration.json")
    if not cal_data:
        return {
            "total_resolved": 0, "active": False, "bets_until_active": 200,
            "edge_buckets": {}, "bet_types": {}, "confidence_tiers": {},
            "adjustments": {},
        }
    return {
        "total_resolved": cal_data.get("total_resolved", 0),
        "active": cal_data.get("active", False),
        "bets_until_active": max(0, 200 - cal_data.get("total_resolved", 0)),
        "edge_buckets": cal_data.get("edge_buckets", {}),
        "bet_types": cal_data.get("bet_types", {}),
        "confidence_tiers": cal_data.get("confidence_tiers", {}),
        "home_away": cal_data.get("home_away", {}),
        "vegas_accuracy": cal_data.get("vegas_accuracy", {}),
        "adjustments": cal_data.get("adjustments", {}),
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "data_dir": str(DATA_DIR), "exists": DATA_DIR.exists()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
