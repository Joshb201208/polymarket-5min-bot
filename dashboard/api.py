"""NBA Agent Dashboard — FastAPI Backend.

Reads JSON files from the agent's data directory and serves
computed stats, positions, trades, and research data.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Load .env so API health checks can read ODDS_API_KEY, BALLDONTLIE_API_KEY, etc.
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")

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
app = FastAPI(title="Polymarket NBA Agent Dashboard API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
DASHBOARD_PASSKEY = os.environ.get("DASHBOARD_PASSKEY", "201208")
_PASSKEY_HASH = hashlib.sha256(DASHBOARD_PASSKEY.encode()).hexdigest()

# Simple in-memory token store (survives within a single process)
_valid_tokens: set[str] = set()


class LoginRequest(BaseModel):
    passkey: str


def _require_auth(request: Request) -> None:
    """Dependency: reject requests without a valid Bearer token."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth.split(" ", 1)[1]
    if token not in _valid_tokens:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


@app.post("/api/login")
def login(body: LoginRequest) -> dict:
    """Validate passkey and return a session token."""
    incoming_hash = hashlib.sha256(body.passkey.encode()).hexdigest()
    if incoming_hash != _PASSKEY_HASH:
        raise HTTPException(status_code=403, detail="Wrong passkey")
    token = secrets.token_hex(32)
    _valid_tokens.add(token)
    return {"token": token}


@app.post("/api/logout")
def logout(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth.split(" ", 1)[1]
        _valid_tokens.discard(token)
    return {"ok": True}

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

@app.get("/api/status", dependencies=[Depends(_require_auth)])
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

    # Last scan time — prefer system_status.json (written each tick)
    system_status = _read_json("system_status.json")
    last_scan = system_status.get("nba_last_scan")
    # Fallback: most recent trade timestamp
    if not last_scan and trades:
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


@app.get("/api/positions", dependencies=[Depends(_require_auth)])
def get_positions() -> dict:
    positions = _read_json("positions.json").get("positions", [])
    open_pos = [p for p in positions if p.get("status") == "open"]
    closed_pos = [p for p in positions if p.get("status") != "open"]
    return {"open": open_pos, "closed": closed_pos}


@app.get("/api/live", dependencies=[Depends(_require_auth)])
def get_live_data() -> dict:
    """Fetch live prices from Polymarket CLOB and scores from ESPN."""
    import urllib.request

    positions = _read_json("positions.json").get("positions", [])
    open_pos = [p for p in positions if p.get("status") == "open"]

    # --- Fetch live market prices from Polymarket Gamma API ---
    # CLOB API is geoblocked from Bengaluru VPS, so use Gamma API
    # which returns outcomePrices per market
    prices = {}
    # Deduplicate market IDs
    market_ids = list({p.get("market_id", "") for p in open_pos if p.get("market_id")})
    market_prices = {}  # token_id -> price

    for mkt_id in market_ids:
        try:
            url = f"https://gamma-api.polymarket.com/markets/{mkt_id}"
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                m = json.loads(resp.read())
                try:
                    outcome_prices = json.loads(m.get("outcomePrices", "[]") or "[]")
                except (json.JSONDecodeError, TypeError):
                    outcome_prices = []
                tokens_raw = m.get("clobTokenIds", "") or ""
                try:
                    token_list = json.loads(tokens_raw) if isinstance(tokens_raw, str) else (tokens_raw or [])
                except (json.JSONDecodeError, TypeError):
                    token_list = []
                for i, tok in enumerate(token_list):
                    if i < len(outcome_prices):
                        try:
                            market_prices[tok] = float(outcome_prices[i])
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass

    # Map token prices to position IDs
    for p in open_pos:
        tok = p.get("token_id", "")
        if tok in market_prices:
            prices[p["id"]] = market_prices[tok]

    # --- Fetch live NBA scores from ESPN ---
    scores = {}
    try:
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            espn = json.loads(resp.read())
            for event in espn.get("events", []):
                # Get team names and scores
                comps = event.get("competitions", [{}])[0]
                teams = comps.get("competitors", [])
                status_obj = comps.get("status", {})
                status_type = status_obj.get("type", {})
                status_name = status_type.get("name", "")  # STATUS_SCHEDULED, STATUS_IN_PROGRESS, STATUS_FINAL
                status_detail = status_obj.get("type", {}).get("shortDetail", status_obj.get("displayClock", ""))
                # Try to get a better detail string
                status_detail = status_type.get("shortDetail", "")
                if not status_detail:
                    status_detail = status_obj.get("displayClock", "")
                    period = status_obj.get("period", 0)
                    if period > 0 and status_name == "STATUS_IN_PROGRESS":
                        status_detail = f"Q{period} {status_detail}"

                home_team = ""
                away_team = ""
                home_score = 0
                away_score = 0
                for t in teams:
                    name = t.get("team", {}).get("displayName", "")
                    score = int(t.get("score", 0) or 0)
                    if t.get("homeAway") == "home":
                        home_team = name
                        home_score = score
                    else:
                        away_team = name
                        away_score = score

                game_key = f"{away_team} vs. {home_team}".lower()
                scores[game_key] = {
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_score": home_score,
                    "away_score": away_score,
                    "status": status_name,
                    "detail": status_detail,
                }
    except Exception as e:
        pass  # scores remain empty

    # --- Match scores to positions ---
    result = []
    for p in open_pos:
        q = (p.get("market_question") or "").lower()
        live_price = prices.get(p["id"])
        current_value = None
        pnl_live = None
        if live_price and live_price > 0:
            current_value = round(p.get("shares", 0) * live_price, 2)
            pnl_live = round(current_value - p.get("cost", 0), 2)

        # Try to match ESPN score
        matched_score = None
        for game_key, score_data in scores.items():
            # Match by checking if both team names appear in the question
            home_short = score_data["home_team"].split()[-1].lower()  # e.g. "Celtics"
            away_short = score_data["away_team"].split()[-1].lower()
            if home_short in q and away_short in q:
                matched_score = score_data
                break

        result.append({
            "id": p["id"],
            "market_question": p.get("market_question"),
            "live_price": live_price,
            "entry_price": p.get("entry_price"),
            "cost": p.get("cost"),
            "shares": p.get("shares"),
            "current_value": current_value,
            "pnl_live": pnl_live,
            "score": matched_score,
        })

    return {"positions": result}


@app.get("/api/trades", dependencies=[Depends(_require_auth)])
def get_trades() -> dict:
    trades = _read_json("trades.json").get("trades", [])
    return {"trades": trades}


@app.get("/api/stats", dependencies=[Depends(_require_auth)])
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

    # Equity curve — build daily P&L and trade counts
    daily_map: dict[str, float] = defaultdict(float)
    daily_count: dict[str, int] = defaultdict(int)
    for p in closed:
        exit_ts = _parse_ts(p.get("exit_time"))
        if exit_ts:
            # Use SGT (UTC+8) for daily grouping
            from datetime import timedelta as _td
            _sgt = timezone(_td(hours=8))
            exit_sgt = exit_ts.astimezone(_sgt)
            day = exit_sgt.strftime("%Y-%m-%d")
            daily_map[day] += p.get("pnl", 0) or 0
            daily_count[day] += 1

    sorted_days = sorted(daily_map.keys())
    equity_curve = []
    running = starting
    for day in sorted_days:
        running += daily_map[day]
        equity_curve.append({
            "date": day,
            "pnl": round(daily_map[day], 2),
            "bankroll": round(running, 2),
            "trades": daily_count[day],
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

    # Total fees paid across all positions (open + closed)
    all_positions = _read_json("positions.json").get("positions", [])
    total_fees = round(sum(p.get("fees_paid", 0) or 0 for p in all_positions), 2)

    return {
        "total_trades": total_closed,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "total_fees": total_fees,
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


@app.get("/api/research", dependencies=[Depends(_require_auth)])
def get_research() -> dict:
    data = _read_json("research_log.json")
    research = data.get("research", [])
    # Sort by game_time descending
    research.sort(key=lambda r: r.get("game_time", ""), reverse=True)
    return {"research": research}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/api/performance/{period}", dependencies=[Depends(_require_auth)])
def get_performance(period: str) -> dict:
    """Return performance stats for a given period: today, week, month, all."""
    positions = _read_json("positions.json").get("positions", [])
    bankroll_data = _read_json("bankroll.json")
    starting = bankroll_data.get("starting_bankroll", 440.58)

    closed = [p for p in positions if p.get("status") != "open"]

    # Filter by period — use Singapore time (UTC+8) for "today"
    from datetime import timedelta as td
    SGT = timezone(td(hours=8))
    now_sgt = datetime.now(SGT)
    now = datetime.now(timezone.utc)

    if period == "today":
        # Start of today in SGT, converted to UTC
        sgt_midnight = now_sgt.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = sgt_midnight.astimezone(timezone.utc)
    elif period == "week":
        cutoff = now - td(days=7)
    elif period == "month":
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


@app.get("/api/calibration", dependencies=[Depends(_require_auth)])
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


@app.get("/api/analytics", dependencies=[Depends(_require_auth)])
def get_analytics() -> dict:
    """Analytics data: entry timing, price drift, opponent strength, P&L by tier."""
    import urllib.request as _urlreq

    positions = _read_json("positions.json").get("positions", [])
    closed = [p for p in positions if p.get("status") != "open"]

    # --- Entry timing vs outcome ---
    timing_data = []
    for p in closed:
        hours = p.get("hours_before_tipoff")
        if hours is not None:
            timing_data.append({
                "hours": hours,
                "won": (p.get("pnl") or 0) > 0,
                "pnl": p.get("pnl", 0),
                "market": p.get("market_question", ""),
            })

    # --- Price drift ---
    drift_data = []
    for p in closed:
        gametime_price = p.get("price_at_gametime")
        if gametime_price is not None:
            entry = p.get("entry_price", 0)
            drift = round((gametime_price - entry) * 100, 1)  # in cents
            drift_data.append({
                "entry_price": entry,
                "gametime_price": gametime_price,
                "drift_cents": drift,
                "won": (p.get("pnl") or 0) > 0,
                "market": p.get("market_question", ""),
            })

    # --- Opponent strength ---
    opp_data = []
    for p in closed:
        opp_wr = p.get("opponent_win_pct")
        if opp_wr is not None:
            opp_data.append({
                "opponent_win_pct": opp_wr,
                "won": (p.get("pnl") or 0) > 0,
                "pnl": p.get("pnl", 0),
                "market": p.get("market_question", ""),
            })

    # --- P&L by confidence tier ---
    tier_pnl = {}
    for p in closed:
        conf = p.get("confidence", "LOW")
        if conf not in tier_pnl:
            tier_pnl[conf] = {"wins": 0, "losses": 0, "pnl": 0, "trades": 0, "wagered": 0}
        tier_pnl[conf]["trades"] += 1
        tier_pnl[conf]["pnl"] += p.get("pnl", 0) or 0
        tier_pnl[conf]["wagered"] += p.get("cost", 0) or 0
        if (p.get("pnl") or 0) > 0:
            tier_pnl[conf]["wins"] += 1
        else:
            tier_pnl[conf]["losses"] += 1
    for v in tier_pnl.values():
        v["pnl"] = round(v["pnl"], 2)
        v["wagered"] = round(v["wagered"], 2)

    # --- P&L by entry price range ---
    range_pnl = {"<15c": {"w": 0, "l": 0, "pnl": 0}, "15-30c": {"w": 0, "l": 0, "pnl": 0},
                 "30-50c": {"w": 0, "l": 0, "pnl": 0}, ">50c": {"w": 0, "l": 0, "pnl": 0}}
    for p in closed:
        ep = p.get("entry_price", 0)
        pnl_val = p.get("pnl", 0) or 0
        if ep < 0.15:
            bucket = "<15c"
        elif ep < 0.30:
            bucket = "15-30c"
        elif ep < 0.50:
            bucket = "30-50c"
        else:
            bucket = ">50c"
        range_pnl[bucket]["pnl"] += pnl_val
        if pnl_val > 0:
            range_pnl[bucket]["w"] += 1
        else:
            range_pnl[bucket]["l"] += 1
    for v in range_pnl.values():
        v["pnl"] = round(v["pnl"], 2)

    return {
        "entry_timing": timing_data,
        "price_drift": drift_data,
        "opponent_strength": opp_data,
        "pnl_by_tier": tier_pnl,
        "pnl_by_price_range": range_pnl,
    }


@app.get("/api/api-health", dependencies=[Depends(_require_auth)])
def get_api_health() -> dict:
    """Check connectivity to all external data sources."""
    import urllib.request as _urlreq

    sources = {}

    # ESPN
    try:
        req = _urlreq.Request(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            headers={"Accept": "application/json"},
        )
        with _urlreq.urlopen(req, timeout=5) as resp:
            sources["espn"] = {"status": "ok", "code": resp.status}
    except Exception as e:
        sources["espn"] = {"status": "error", "error": str(e)[:80]}

    # The Odds API
    odds_key = os.environ.get("ODDS_API_KEY", "")
    if odds_key:
        try:
            req = _urlreq.Request(
                f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds/?apiKey={odds_key}&regions=us&markets=h2h&oddsFormat=decimal&bookmakers=draftkings",
                headers={"Accept": "application/json"},
            )
            with _urlreq.urlopen(req, timeout=8) as resp:
                remaining = resp.headers.get("x-requests-remaining", "?")
                sources["odds_api"] = {"status": "ok", "credits_remaining": remaining}
        except Exception as e:
            sources["odds_api"] = {"status": "error", "error": str(e)[:80]}
    else:
        sources["odds_api"] = {"status": "no_key"}

    # BallDontLie
    bdl_key = os.environ.get("BALLDONTLIE_API_KEY", "")
    if bdl_key:
        try:
            req = _urlreq.Request(
                "https://api.balldontlie.io/v1/teams",
                headers={"Accept": "application/json", "Authorization": bdl_key},
            )
            with _urlreq.urlopen(req, timeout=5) as resp:
                sources["balldontlie"] = {"status": "ok", "code": resp.status}
        except Exception as e:
            sources["balldontlie"] = {"status": "error", "error": str(e)[:80]}
    else:
        sources["balldontlie"] = {"status": "no_key"}

    # Polymarket Gamma API
    try:
        req = _urlreq.Request(
            "https://gamma-api.polymarket.com/markets?limit=1&active=true",
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        with _urlreq.urlopen(req, timeout=5) as resp:
            sources["polymarket"] = {"status": "ok", "code": resp.status}
    except Exception as e:
        sources["polymarket"] = {"status": "error", "error": str(e)[:80]}

    return {"sources": sources}


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "data_dir": str(DATA_DIR), "exists": DATA_DIR.exists()}


@app.post("/api/deploy", dependencies=[Depends(_require_auth)])
def deploy() -> dict:
    """Pull latest code from GitHub and restart the agent service."""
    import subprocess

    project_dir = Path("/root/polymarket-bot")
    results = {}

    # Git fetch + reset (clean refs first to avoid corruption)
    try:
        # Remove stale ref file that causes "unable to update local ref"
        ref_file = project_dir / ".git" / "refs" / "remotes" / "origin" / "master"
        if ref_file.exists():
            ref_file.unlink()
        # Prune stale remote refs
        subprocess.run(
            ["git", "remote", "prune", "origin"],
            cwd=project_dir, capture_output=True, timeout=10,
        )
        fetch = subprocess.run(
            ["git", "fetch", "--all"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        reset = subprocess.run(
            ["git", "reset", "--hard", "origin/master"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=15,
        )
        results["git_pull"] = {
            "ok": fetch.returncode == 0 and reset.returncode == 0,
            "stdout": reset.stdout.strip()[-500:],
            "stderr": (fetch.stderr.strip() + reset.stderr.strip())[-200:] if (fetch.returncode != 0 or reset.returncode != 0) else "",
        }
    except Exception as e:
        results["git_pull"] = {"ok": False, "error": str(e)[:200]}

    # Reload nginx to pick up any static file changes
    try:
        nginx_reload = subprocess.run(
            ["systemctl", "reload", "nginx"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        results["nginx_reload"] = {"ok": nginx_reload.returncode == 0}
    except Exception as e:
        results["nginx_reload"] = {"ok": False, "error": str(e)[:100]}

    # Restart agent service (dashboard restarts itself via auto_update cron)
    try:
        restart = subprocess.run(
            ["systemctl", "restart", "nba-agent"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        results["restart_nba-agent"] = {
            "ok": restart.returncode == 0,
            "stderr": restart.stderr.strip()[-200:] if restart.returncode != 0 else "",
        }
    except Exception as e:
        results["restart_nba-agent"] = {"ok": False, "error": str(e)[:200]}

    results["status"] = "deployed" if all(
        r.get("ok") for r in results.values()
    ) else "partial"

    return results


@app.get("/api/system-health", dependencies=[Depends(_require_auth)])
def get_system_health() -> dict:
    """Return last scan times, API status, uptime info."""
    import urllib.request as _urlreq

    bankroll = _read_json("bankroll.json")

    # Primary source: system_status.json written by agent each tick
    system_status = _read_json("system_status.json")
    nba_last_scan = system_status.get("nba_last_scan")

    # Fallback: derive from trade timestamps if system_status not yet populated
    if not nba_last_scan:
        nba_trades = _read_json("trades.json").get("trades", [])
        if nba_trades:
            timestamps = [t.get("timestamp") for t in nba_trades if t.get("timestamp")]
            if timestamps:
                nba_last_scan = max(timestamps)

    # Uptime from earliest trade
    nba_trades = _read_json("trades.json").get("trades", [])
    uptime_hours = 0.0
    if nba_trades:
        timestamps = [_parse_ts(t.get("timestamp")) for t in nba_trades]
        timestamps = [t for t in timestamps if t is not None]
        if timestamps:
            earliest = min(timestamps)
            uptime_hours = round(
                (datetime.now(timezone.utc) - earliest).total_seconds() / 3600, 1
            )

    # Odds API credits remaining
    odds_api_credits = system_status.get("odds_api_credits")
    if odds_api_credits is None:
        odds_key = os.environ.get("ODDS_API_KEY", "")
        if odds_key:
            try:
                req = _urlreq.Request(
                    f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds/?apiKey={odds_key}&regions=us&markets=h2h&oddsFormat=decimal&bookmakers=draftkings",
                    headers={"Accept": "application/json"},
                )
                with _urlreq.urlopen(req, timeout=5) as resp:
                    odds_api_credits = int(resp.headers.get("x-requests-remaining", 0))
            except Exception:
                odds_api_credits = None

    # Quick API health checks
    apis = {}
    try:
        req = _urlreq.Request(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            headers={"Accept": "application/json"},
        )
        with _urlreq.urlopen(req, timeout=5) as resp:
            apis["espn"] = resp.status == 200
    except Exception:
        apis["espn"] = False

    try:
        req = _urlreq.Request(
            "https://gamma-api.polymarket.com/markets?limit=1&active=true",
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        with _urlreq.urlopen(req, timeout=5) as resp:
            apis["polymarket"] = resp.status == 200
    except Exception:
        apis["polymarket"] = False

    return {
        "nba_last_scan": nba_last_scan,
        "odds_api_credits": odds_api_credits,
        "uptime_hours": uptime_hours,
        "apis": apis,
        "bankroll": bankroll.get("current_bankroll", 0),
        "is_paused": bankroll.get("is_paused", False),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
