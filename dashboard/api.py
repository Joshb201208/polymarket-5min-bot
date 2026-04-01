"""Events Trading Agent Dashboard — FastAPI Backend.

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

# Load .env
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")

# ---------------------------------------------------------------------------
# Data directory — /root/polymarket-bot/data/ on VPS, ./data/ locally
# ---------------------------------------------------------------------------
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
app = FastAPI(title="Events Trading Agent Dashboard API", version="4.0.0")

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

def _is_real_trade(p: dict) -> bool:
    """Return True if this position is a real trade (not purge/cleanup/worthless).

    Used to filter out noise from P&L calculations.
    """
    exit_reason = (p.get("exit_reason") or "").lower()
    edge_source = (p.get("edge_source") or "").lower()

    if edge_source == "extreme_pricing":
        return False

    noise_keywords = ("purge", "worthless", "no_shares", "extreme_pricing")
    for kw in noise_keywords:
        if kw in exit_reason:
            return False

    return True


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
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "data_dir": str(DATA_DIR), "exists": DATA_DIR.exists()}


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

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

    # Copy service file + daemon-reload (in case service config changed)
    try:
        svc_src = project_dir / "deploy" / "agents.service"
        if svc_src.exists():
            subprocess.run(
                ["cp", str(svc_src), "/etc/systemd/system/events-agent.service"],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["systemctl", "daemon-reload"],
                capture_output=True, timeout=5,
            )
            results["service_updated"] = {"ok": True}
    except Exception as e:
        results["service_updated"] = {"ok": False, "error": str(e)[:100]}

    # Copy updated nginx config + reload
    try:
        nginx_src = project_dir / "dashboard" / "nginx.conf"
        if nginx_src.exists():
            subprocess.run(
                ["cp", str(nginx_src), "/etc/nginx/sites-available/dashboard"],
                capture_output=True, timeout=5,
            )
        nginx_reload = subprocess.run(
            ["systemctl", "reload", "nginx"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        results["nginx_reload"] = {"ok": nginx_reload.returncode == 0}
    except Exception as e:
        results["nginx_reload"] = {"ok": False, "error": str(e)[:100]}

    # Restart agent service
    try:
        restart = subprocess.run(
            ["systemctl", "restart", "events-agent"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        results["restart_events-agent"] = {
            "ok": restart.returncode == 0,
            "stderr": restart.stderr.strip()[-200:] if restart.returncode != 0 else "",
        }
    except Exception as e:
        results["restart_events-agent"] = {"ok": False, "error": str(e)[:200]}

    # Restart dashboard service (picks up new API code)
    # Uses subprocess.Popen to avoid blocking — the current process will be killed
    try:
        subprocess.Popen(
            ["systemctl", "restart", "dashboard"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        results["restart_dashboard"] = {"ok": True}
    except Exception as e:
        results["restart_dashboard"] = {"ok": False, "error": str(e)[:200]}

    results["status"] = "deployed" if all(
        r.get("ok") for r in results.values()
    ) else "partial"

    return results


# ===========================================================================
# Events Agent Endpoints
# ===========================================================================

# Events agent starting bankroll — ground truth after extreme_pricing damage.
# Original deposit $440.58 minus ~$198 locked in unsellable junk positions.
EVENTS_STARTING_BANKROLL = 242.11


@app.get("/api/events/status", dependencies=[Depends(_require_auth)])
def get_events_status() -> dict:
    """Status for events agent."""
    system_status = _read_json("system_status.json")
    bankroll = _read_json("events_bankroll.json")
    events_positions = _read_json("events_positions.json").get("positions", [])
    events_trades = _read_json("events_trades.json").get("trades", [])

    mode = "paper"
    if events_trades:
        mode = events_trades[-1].get("mode", "paper")

    return {
        "mode": mode,
        "events_last_scan": system_status.get("events_last_scan"),
        "open_positions": len([p for p in events_positions if p.get("status") == "open"]),
        "total_positions": len(events_positions),
        "bankroll": bankroll.get("current_bankroll", 242.11),
    }


@app.get("/api/events/positions", dependencies=[Depends(_require_auth)])
def get_events_positions() -> dict:
    """Events positions (open + closed, filtered to real trades)."""
    positions = _read_json("events_positions.json").get("positions", [])
    open_pos = [p for p in positions if p.get("status") == "open"]
    closed_pos = [p for p in positions if p.get("status") != "open" and _is_real_trade(p)]
    return {"open": open_pos, "closed": closed_pos}


@app.get("/api/events/trades", dependencies=[Depends(_require_auth)])
def get_events_trades() -> dict:
    """Events trades + closed positions for history table.

    Filters out purge/cleanup/worthless entries so the dashboard shows
    only real trades, sorted most-recent first.
    """
    trades_raw = _read_json("events_trades.json").get("trades", [])
    positions = _read_json("events_positions.json").get("positions", [])

    # Build set of position IDs that are purge/cleanup (not real trades)
    noise_position_ids = set()
    for p in positions:
        if not _is_real_trade(p):
            noise_position_ids.add(p.get("id", ""))

    # Filter trades: exclude those linked to noise positions
    clean_trades = [
        t for t in trades_raw
        if t.get("position_id", "") not in noise_position_ids
    ]

    # Sort most recent first
    clean_trades.sort(key=lambda t: t.get("timestamp", ""), reverse=True)

    closed = [p for p in positions if p.get("status") != "open" and _is_real_trade(p)]
    closed.sort(key=lambda p: p.get("exit_time", ""), reverse=True)

    return {"trades": clean_trades, "closed_positions": closed}


@app.get("/api/events/stats", dependencies=[Depends(_require_auth)])
def get_events_stats() -> dict:
    """Events stats — P&L, win rate, category breakdown, exit analysis."""
    positions = _read_json("events_positions.json").get("positions", [])
    starting = EVENTS_STARTING_BANKROLL

    # Filter to real trades only — exclude purge/cleanup/worthless/extreme_pricing
    closed = [p for p in positions if p.get("status") != "open" and _is_real_trade(p)]

    total_closed = len(closed)
    wins = [p for p in closed if (p.get("pnl") or 0) > 0]
    losses = [p for p in closed if (p.get("pnl") or 0) <= 0]
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = round((win_count / total_closed * 100) if total_closed > 0 else 0, 1)

    pnls = [p.get("pnl", 0) or 0 for p in closed]
    total_pnl = round(sum(pnls), 2)
    total_invested = sum(p.get("cost", 0) or 0 for p in closed)
    roi = round((total_pnl / starting * 100) if starting > 0 else 0, 1)

    # Category breakdown (count of all positions by category)
    category_breakdown: dict[str, int] = {}
    for p in positions:
        cat = p.get("category", "other")
        category_breakdown[cat] = category_breakdown.get(cat, 0) + 1

    # Exit reason breakdown
    exit_reasons: dict[str, int] = {}
    for p in closed:
        reason = p.get("exit_reason", "Unknown")
        # Simplify reason
        if "take profit" in reason.lower() or "Take profit" in reason:
            key = "Take profit"
        elif "stop loss" in reason.lower() or "Stop loss" in reason:
            key = "Stop loss"
        elif "WIN" in reason:
            key = "Market resolved: WIN"
        elif "LOSS" in reason:
            key = "Market resolved: LOSS"
        elif "liquidity" in reason.lower():
            key = "Low liquidity exit"
        else:
            key = reason[:30]
        exit_reasons[key] = exit_reasons.get(key, 0) + 1

    # Average hold time (hours) for closed positions
    avg_hold_hours = None
    hold_times = []
    for p in closed:
        entry = _parse_ts(p.get("entry_time"))
        exit_t = _parse_ts(p.get("exit_time"))
        if entry and exit_t:
            hours = (exit_t - entry).total_seconds() / 3600
            hold_times.append(hours)
    if hold_times:
        avg_hold_hours = round(sum(hold_times) / len(hold_times), 1)

    return {
        "total_trades": total_closed,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "roi": roi,
        "category_breakdown": category_breakdown,
        "exit_reasons": exit_reasons,
        "avg_hold_hours": avg_hold_hours,
    }


@app.get("/api/events/portfolio_value", dependencies=[Depends(_require_auth)])
def get_events_portfolio_value() -> dict:
    """Mark-to-market portfolio value: cash + current value of all positions.

    Recalculates from first principles every call using the events-specific
    starting bankroll ($242.11).
    """
    import urllib.request as _urlreq

    starting = EVENTS_STARTING_BANKROLL

    positions = _read_json("events_positions.json").get("positions", [])
    open_pos = [p for p in positions if p.get("status") == "open"]
    closed_pos = [p for p in positions if p.get("status") != "open"]

    # --- Recalculate bankroll from first principles (same as agent) ---
    real_closed = [p for p in closed_pos if _is_real_trade(p)]
    realized_pnl = sum(p.get("pnl", 0) or 0 for p in real_closed)
    current_bankroll = starting + realized_pnl

    # Fetch live prices for open positions
    market_ids = list({p.get("market_id", "") for p in open_pos if p.get("market_id")})
    market_prices: dict[str, float] = {}
    for mkt_id in market_ids:
        try:
            url = f"https://gamma-api.polymarket.com/markets/{mkt_id}"
            req = _urlreq.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            with _urlreq.urlopen(req, timeout=8) as resp:
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

    # Calculate position values
    deployed = 0.0
    current_value_total = 0.0
    unrealized_pnl = 0.0
    positions_up = 0
    positions_down = 0
    best_pos = None
    best_pnl_pct = -999
    worst_pos = None
    worst_pnl_pct = 999

    for p in open_pos:
        cost = p.get("cost", 0) or 0
        deployed += cost
        tok = p.get("token_id", "")
        shares = p.get("shares", 0) or 0
        live_price = market_prices.get(tok)

        if live_price and shares > 0:
            cv = shares * live_price
            pnl = cv - cost
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0
            current_value_total += cv
            unrealized_pnl += pnl
            if pnl >= 0:
                positions_up += 1
            else:
                positions_down += 1
            if pnl_pct > best_pnl_pct:
                best_pnl_pct = pnl_pct
                best_pos = {"question": (p.get("market_question") or "")[:50], "pnl_pct": round(pnl_pct, 1)}
            if pnl_pct < worst_pnl_pct:
                worst_pnl_pct = pnl_pct
                worst_pos = {"question": (p.get("market_question") or "")[:50], "pnl_pct": round(pnl_pct, 1)}
        else:
            current_value_total += cost

    cash = max(0, current_bankroll - deployed)
    portfolio_value = cash + current_value_total

    # Avg hold time for open positions
    avg_hold_hours = None
    hold_times = []
    now = datetime.now(timezone.utc)
    for p in open_pos:
        entry = _parse_ts(p.get("entry_time"))
        if entry:
            hold_times.append((now - entry).total_seconds() / 3600)
    if hold_times:
        avg_hold_hours = round(sum(hold_times) / len(hold_times), 1)

    return {
        "portfolio_value": round(portfolio_value, 2),
        "starting_bankroll": starting,
        "current_bankroll": round(current_bankroll, 2),
        "cash": round(cash, 2),
        "cash_pct": round(cash / current_bankroll * 100, 1) if current_bankroll > 0 else 100,
        "deployed": round(deployed, 2),
        "deployed_pct": round(deployed / current_bankroll * 100, 1) if current_bankroll > 0 else 0,
        "unrealized_pnl": round(unrealized_pnl, 2),
        "unrealized_pnl_pct": round(unrealized_pnl / deployed * 100, 1) if deployed > 0 else 0,
        "realized_pnl": round(realized_pnl, 2),
        "positions_up": positions_up,
        "positions_down": positions_down,
        "active_positions": len(open_pos),
        "best_position": best_pos,
        "worst_position": worst_pos,
        "avg_hold_hours": avg_hold_hours,
    }


@app.get("/api/events/unrealized", dependencies=[Depends(_require_auth)])
def get_events_unrealized() -> dict:
    """Per-position unrealized P&L with current prices."""
    import urllib.request as _urlreq

    positions = _read_json("events_positions.json").get("positions", [])
    open_pos = [p for p in positions if p.get("status") == "open"]

    # Fetch live prices
    market_ids = list({p.get("market_id", "") for p in open_pos if p.get("market_id")})
    market_prices: dict[str, float] = {}
    for mkt_id in market_ids:
        try:
            url = f"https://gamma-api.polymarket.com/markets/{mkt_id}"
            req = _urlreq.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            with _urlreq.urlopen(req, timeout=8) as resp:
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

    now = datetime.now(timezone.utc)
    result = []
    for p in open_pos:
        tok = p.get("token_id", "")
        live_price = market_prices.get(tok)
        cost = p.get("cost", 0) or 0
        shares = p.get("shares", 0) or 0
        entry_price = p.get("entry_price", 0) or 0

        current_value = None
        pnl = None
        pnl_pct = None
        if live_price and shares > 0:
            current_value = round(shares * live_price, 2)
            pnl = round(current_value - cost, 2)
            pnl_pct = round(pnl / cost * 100, 1) if cost > 0 else 0

        # Hold duration
        hold_hours = None
        entry_ts = _parse_ts(p.get("entry_time"))
        if entry_ts:
            hold_hours = round((now - entry_ts).total_seconds() / 3600, 1)

        result.append({
            "id": p.get("id"),
            "market_id": p.get("market_id"),
            "market_question": p.get("market_question"),
            "category": p.get("category", "other"),
            "side": p.get("side"),
            "entry_price": entry_price,
            "current_price": live_price,
            "cost": cost,
            "shares": shares,
            "current_value": current_value,
            "unrealized_pnl": pnl,
            "unrealized_pnl_pct": pnl_pct,
            "peak_pnl_pct": p.get("peak_pnl_pct"),
            "trailing_stop_pct": p.get("trailing_stop_pct"),
            "hold_hours": hold_hours,
            "entry_time": p.get("entry_time"),
            "entry_signals": p.get("entry_signals", []),
            "entry_composite": p.get("entry_composite"),
            "last_composite": p.get("last_composite"),
            "confidence": p.get("confidence"),
            "edge_at_entry": p.get("edge_at_entry"),
            "edge_source": p.get("edge_source"),
        })

    return {"positions": result}


@app.get("/api/events/position/{position_id}", dependencies=[Depends(_require_auth)])
def get_events_position_detail(position_id: str) -> dict:
    """Deep-dive single position with full history, signals, exit status."""
    import urllib.request as _urlreq

    positions = _read_json("events_positions.json").get("positions", [])
    position = None
    for p in positions:
        if p.get("id") == position_id:
            position = p
            break

    if not position:
        raise HTTPException(status_code=404, detail="Position not found")

    # Fetch live price
    live_price = None
    mkt_id = position.get("market_id", "")
    tok = position.get("token_id", "")
    if mkt_id:
        try:
            url = f"https://gamma-api.polymarket.com/markets/{mkt_id}"
            req = _urlreq.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            with _urlreq.urlopen(req, timeout=8) as resp:
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
                for i, t in enumerate(token_list):
                    if t == tok and i < len(outcome_prices):
                        try:
                            live_price = float(outcome_prices[i])
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass

    # Calculate unrealized P&L
    cost = position.get("cost", 0) or 0
    shares = position.get("shares", 0) or 0
    current_value = None
    unrealized_pnl = None
    unrealized_pnl_pct = None
    if live_price and shares > 0:
        current_value = round(shares * live_price, 2)
        unrealized_pnl = round(current_value - cost, 2)
        unrealized_pnl_pct = round(unrealized_pnl / cost * 100, 1) if cost > 0 else 0

    # Lifecycle & regime
    lc_data = _read_json("lifecycle_assessments.json")
    rg_data = _read_json("regime_assessments.json")
    lifecycle = (lc_data.get("assessments", {}) or {}).get(mkt_id, {})
    regime = (rg_data.get("assessments", {}) or {}).get(mkt_id, {})

    # Hold duration
    now = datetime.now(timezone.utc)
    entry_ts = _parse_ts(position.get("entry_time"))
    hold_hours = round((now - entry_ts).total_seconds() / 3600, 1) if entry_ts else None

    return {
        **position,
        "live_price": live_price,
        "current_value": current_value,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "hold_hours": hold_hours,
        "lifecycle": lifecycle,
        "regime": regime,
    }


@app.get("/api/events/exit_status", dependencies=[Depends(_require_auth)])
def get_events_exit_status() -> dict:
    """Exit proximity for all open positions."""
    import urllib.request as _urlreq

    positions = _read_json("events_positions.json").get("positions", [])
    open_pos = [p for p in positions if p.get("status") == "open"]

    # Fetch live prices
    market_ids = list({p.get("market_id", "") for p in open_pos if p.get("market_id")})
    market_prices: dict[str, float] = {}
    for mkt_id in market_ids:
        try:
            url = f"https://gamma-api.polymarket.com/markets/{mkt_id}"
            req = _urlreq.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            with _urlreq.urlopen(req, timeout=8) as resp:
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

    result = []
    for p in open_pos:
        tok = p.get("token_id", "")
        live_price = market_prices.get(tok)
        cost = p.get("cost", 0) or 0
        shares = p.get("shares", 0) or 0

        pnl_pct = 0
        if live_price and shares > 0 and cost > 0:
            cv = shares * live_price
            pnl_pct = (cv - cost) / cost * 100

        # Determine zone
        if pnl_pct >= 15:
            zone = "tp_zone"
            zone_label = "TP Zone"
            zone_icon = "\U0001f7e2"
        elif pnl_pct <= -15:
            zone = "sl_zone"
            zone_label = "SL Zone"
            zone_icon = "\U0001f534"
        else:
            zone = "monitoring"
            zone_label = "Monitoring"
            zone_icon = "\U0001f7e1"

        result.append({
            "id": p.get("id"),
            "market_question": (p.get("market_question") or "")[:60],
            "pnl_pct": round(pnl_pct, 1),
            "zone": zone,
            "zone_label": zone_label,
            "zone_icon": zone_icon,
            "entry_price": p.get("entry_price"),
            "current_price": live_price,
            "cost": cost,
            "peak_pnl_pct": p.get("peak_pnl_pct"),
            "trailing_stop_pct": p.get("trailing_stop_pct"),
        })

    return {"positions": result}


@app.get("/api/events/categories", dependencies=[Depends(_require_auth)])
def get_events_categories() -> dict:
    """Category-level P&L and position counts."""
    positions = _read_json("events_positions.json").get("positions", [])
    real_positions = [p for p in positions if _is_real_trade(p) or p.get("status") == "open"]

    categories: dict[str, dict] = {}
    for p in real_positions:
        cat = p.get("category", "other")
        if cat not in categories:
            categories[cat] = {"open": 0, "closed": 0, "pnl": 0.0, "invested": 0.0}
        if p.get("status") == "open":
            categories[cat]["open"] += 1
            categories[cat]["invested"] += p.get("cost", 0) or 0
        else:
            categories[cat]["closed"] += 1
            categories[cat]["pnl"] += p.get("pnl", 0) or 0
            categories[cat]["invested"] += p.get("cost", 0) or 0

    result = []
    for cat, data in sorted(categories.items(), key=lambda x: -x[1]["invested"]):
        roi = round(data["pnl"] / data["invested"] * 100, 1) if data["invested"] > 0 else 0
        result.append({
            "category": cat,
            "open": data["open"],
            "closed": data["closed"],
            "pnl": round(data["pnl"], 2),
            "invested": round(data["invested"], 2),
            "roi": roi,
        })

    return {"categories": result}


@app.get("/api/events/ticker", dependencies=[Depends(_require_auth)])
def get_events_ticker() -> dict:
    """Real-time ticker: last 20 events across trades and position changes."""
    trades = _read_json("events_trades.json").get("trades", [])
    positions = _read_json("events_positions.json").get("positions", [])

    events = []

    # Recent trades
    for t in trades[-30:]:
        action = t.get("action", "BUY")
        question = (t.get("market_question") or "")[:50]
        cost = t.get("cost", 0) or 0
        price = t.get("price", 0) or 0
        price_cents = f"{price * 100:.1f}¢"
        pnl = t.get("pnl")

        if action == "BUY":
            desc = f"BUY {question} @ {price_cents}"
            evt_type = "buy"
        else:
            pnl_str = f"+${pnl:.2f}" if pnl and pnl > 0 else f"${pnl:.2f}" if pnl else ""
            desc = f"SELL {question} {pnl_str}"
            evt_type = "sell"

        events.append({
            "time": t.get("timestamp"),
            "type": evt_type,
            "description": desc,
            "amount": round(cost, 2) if action == "BUY" else round(abs(pnl or 0), 2),
        })

    events.sort(key=lambda e: e["time"] or "", reverse=True)
    return {"events": events[:20]}


@app.get("/api/events/trade/{trade_id}", dependencies=[Depends(_require_auth)])
def get_events_trade_detail(trade_id: str) -> dict:
    """Get full details for a single trade."""
    trades = _read_json("events_trades.json").get("trades", [])
    for t in trades:
        if t.get("id") == trade_id or t.get("position_id") == trade_id:
            return t
    raise HTTPException(status_code=404, detail="Trade not found")


@app.post("/api/events/close/{position_id}", dependencies=[Depends(_require_auth)])
def close_events_position(position_id: str) -> dict:
    """Manual close: write a close request file that the agent picks up."""
    close_dir = DATA_DIR / "close_requests"
    close_dir.mkdir(parents=True, exist_ok=True)

    request_file = close_dir / f"{position_id}.json"
    request_file.write_text(json.dumps({
        "position_id": position_id,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "source": "dashboard",
    }))

    return {"status": "queued", "position_id": position_id}


class ResetRequest(BaseModel):
    new_starting_bankroll: float


@app.post("/api/events/reset", dependencies=[Depends(_require_auth)])
def reset_events_pnl(body: ResetRequest) -> dict:
    """Reset P&L tracking: archive old data and start fresh.

    1. Archives events_positions.json and events_trades.json
    2. Creates fresh positions file with only current open positions
    3. Creates fresh empty trades file
    4. Updates events_bankroll.json with new starting_bankroll
    """
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")

    # Archive existing files
    archive_dir = DATA_DIR / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)

    positions_path = DATA_DIR / "events_positions.json"
    trades_path = DATA_DIR / "events_trades.json"
    bankroll_path = DATA_DIR / "events_bankroll.json"

    archived = []
    for src, name in [(positions_path, "events_positions"), (trades_path, "events_trades")]:
        if src.exists():
            dest = archive_dir / f"{name}_{ts}.json"
            dest.write_text(src.read_text())
            archived.append(str(dest.name))

    # Read current open positions to preserve them
    positions_data = {}
    if positions_path.exists():
        try:
            positions_data = json.loads(positions_path.read_text())
        except (json.JSONDecodeError, OSError):
            positions_data = {}

    open_positions = [
        p for p in positions_data.get("positions", [])
        if p.get("status") == "open"
    ]

    # Write fresh positions file (only open positions)
    positions_path.write_text(json.dumps({"positions": open_positions}, indent=2, default=str))

    # Write fresh empty trades file
    trades_path.write_text(json.dumps({"trades": []}, indent=2))

    # Update bankroll
    bankroll_data = {}
    if bankroll_path.exists():
        try:
            bankroll_data = json.loads(bankroll_path.read_text())
        except (json.JSONDecodeError, OSError):
            bankroll_data = {}

    bankroll_data["starting_bankroll"] = body.new_starting_bankroll
    bankroll_data["current_bankroll"] = body.new_starting_bankroll
    bankroll_data["realized_pnl"] = 0.0
    bankroll_data["reset_at"] = now.isoformat()
    bankroll_data["reset_reason"] = "manual_reset_from_dashboard"

    bankroll_path.write_text(json.dumps(bankroll_data, indent=2, default=str))

    return {
        "status": "reset_complete",
        "new_starting_bankroll": body.new_starting_bankroll,
        "open_positions_preserved": len(open_positions),
        "archived_files": archived,
        "reset_at": now.isoformat(),
    }


@app.get("/api/events/lifecycle", dependencies=[Depends(_require_auth)])
def get_events_lifecycle() -> dict:
    """Lifecycle assessments for active markets."""
    data = _read_json("lifecycle_assessments.json")
    return {
        "assessments": data.get("assessments", {}),
        "timestamp": data.get("timestamp"),
    }


@app.get("/api/events/regime", dependencies=[Depends(_require_auth)])
def get_events_regime() -> dict:
    """Regime detection for active markets."""
    data = _read_json("regime_assessments.json")
    return {
        "assessments": data.get("assessments", {}),
        "timestamp": data.get("timestamp"),
    }


# ===========================================================================
# Intelligence Endpoints
# ===========================================================================

@app.get("/api/intelligence/signals", dependencies=[Depends(_require_auth)])
def get_intelligence_signals() -> dict:
    """Latest intelligence signals from all sources."""
    data = _read_json("intelligence_signals.json")
    signals = data.get("signals", []) if isinstance(data, dict) else []

    # Sort by timestamp descending
    signals.sort(key=lambda s: s.get("timestamp", ""), reverse=True)

    return {
        "signals": signals[:100],
        "total": len(signals),
        "timestamp": data.get("timestamp") if isinstance(data, dict) else None,
    }


@app.get("/api/intelligence/health", dependencies=[Depends(_require_auth)])
def get_intelligence_health() -> dict:
    """Health status of all intelligence sources."""
    data = _read_json("intelligence_health.json")
    if not isinstance(data, dict):
        return {"sources": {}, "timestamp": None}
    return {
        "sources": {k: v for k, v in data.items() if k != "timestamp"},
        "timestamp": data.get("timestamp"),
    }


@app.get("/api/intelligence/composite", dependencies=[Depends(_require_auth)])
def get_intelligence_composite() -> dict:
    """Composite scores for active markets."""
    data = _read_json("composite_scores.json")
    scores = data.get("scores", {}) if isinstance(data, dict) else {}
    return {
        "scores": scores,
        "timestamp": data.get("timestamp") if isinstance(data, dict) else None,
    }


@app.get("/api/intelligence/dedup", dependencies=[Depends(_require_auth)])
def get_intelligence_dedup() -> dict:
    """Dedup cluster stats from the latest scan cycle."""
    data = _read_json("dedup_stats.json")
    return {
        "total_raw_signals": data.get("total_raw", 0),
        "total_after_dedup": data.get("total_deduped", 0),
        "clusters": data.get("clusters", []),
        "decay_dropped": data.get("decay_dropped", 0),
        "timestamp": data.get("timestamp"),
    }


# ===========================================================================
# Analytics Endpoints
# ===========================================================================

@app.get("/api/analytics/equity_curve", dependencies=[Depends(_require_auth)])
def get_analytics_equity_curve() -> dict:
    """Equity curve data — events only."""
    bankroll_data = _read_json("bankroll.json")
    starting = bankroll_data.get("starting_bankroll", 242.11)
    from datetime import timedelta as _td

    SGT = timezone(_td(hours=8))

    # Events equity data
    events_positions = _read_json("events_positions.json").get("positions", [])
    events_closed = [p for p in events_positions if p.get("status") != "open" and _is_real_trade(p)]

    events_daily: dict[str, float] = defaultdict(float)
    for p in events_closed:
        exit_ts = _parse_ts(p.get("exit_time"))
        if exit_ts:
            day = exit_ts.astimezone(SGT).strftime("%Y-%m-%d")
            events_daily[day] += p.get("pnl", 0) or 0

    all_days = sorted(events_daily.keys())

    events_curve = []
    events_running = 0

    for day in all_days:
        events_running += events_daily.get(day, 0)
        events_curve.append({"date": day, "value": round(events_running, 2)})

    return {
        "events": events_curve,
        "starting_bankroll": starting,
    }


@app.get("/api/analytics/allocation", dependencies=[Depends(_require_auth)])
def get_analytics_allocation() -> dict:
    """Current bankroll allocation."""
    bankroll_data = _read_json("bankroll.json")
    total_bankroll = bankroll_data.get("current_bankroll", 0)

    # Events open positions
    events_positions = _read_json("events_positions.json").get("positions", [])
    events_open = [p for p in events_positions if p.get("status") == "open"]
    events_deployed = sum(p.get("cost", 0) or 0 for p in events_open)

    cash = max(0, total_bankroll - events_deployed)

    return {
        "total_bankroll": round(total_bankroll, 2),
        "segments": [
            {"label": "Events", "value": round(events_deployed, 2), "color": "#8B5CF6"},
            {"label": "Cash", "value": round(cash, 2), "color": "#374151"},
        ],
        "total_deployed": round(events_deployed, 2),
        "deployed_pct": round(events_deployed / total_bankroll * 100, 1) if total_bankroll > 0 else 0,
    }


@app.get("/api/analytics/risk", dependencies=[Depends(_require_auth)])
def get_analytics_risk() -> dict:
    """Portfolio risk dashboard — exposure, theme concentration, diversification."""
    bankroll_data = _read_json("bankroll.json")
    total_bankroll = bankroll_data.get("current_bankroll", 0) or 1
    max_exposure_pct = float(os.environ.get("MAX_TOTAL_EXPOSURE_PCT", "0.50"))

    # Events exposure
    events_positions = _read_json("events_positions.json").get("positions", [])
    events_open = [p for p in events_positions if p.get("status") == "open"]
    events_exposure = sum(p.get("cost", 0) or 0 for p in events_open)

    total_exposure = events_exposure

    # Theme concentration (from events positions)
    theme_map = {
        "politics": ["trump", "republican", "democrat", "congress", "senate", "election",
                      "biden", "president", "vote", "governor"],
        "crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "defi", "sec",
                    "stablecoin"],
        "geopolitical": ["china", "russia", "ukraine", "taiwan", "nato", "sanctions",
                         "tariff", "war", "missile"],
        "economics": ["fed", "inflation", "recession", "gdp", "unemployment",
                       "interest rate", "treasury"],
    }

    theme_exposure: dict[str, float] = defaultdict(float)
    for p in events_open:
        q = (p.get("market_question", "") or "").lower()
        cost = p.get("cost", 0) or 0
        matched = False
        for theme, keywords in theme_map.items():
            if any(kw in q for kw in keywords):
                theme_exposure[theme] += cost
                matched = True
                break
        if not matched:
            theme_exposure["other"] += cost

    # Diversification score (0-100)
    if total_exposure > 0:
        theme_pcts = [v / total_exposure for v in theme_exposure.values()]
        hhi = sum(p ** 2 for p in theme_pcts) if theme_pcts else 1.0
        n = max(len(theme_pcts), 1)
        min_hhi = 1.0 / n
        div_score = max(0, min(100, int((1.0 - (hhi - min_hhi) / (1.0 - min_hhi + 0.01)) * 100)))
    else:
        div_score = 100

    return {
        "total_bankroll": round(total_bankroll, 2),
        "total_deployed": round(total_exposure, 2),
        "available": round(max(0, total_bankroll - total_exposure), 2),
        "events_exposure": round(events_exposure, 2),
        "events_pct": round(events_exposure / total_bankroll * 100, 1) if total_bankroll else 0,
        "total_pct": round(total_exposure / total_bankroll * 100, 1) if total_bankroll else 0,
        "max_allowed_pct": round(max_exposure_pct * 100, 1),
        "theme_concentration": {
            k: {
                "amount": round(v, 2),
                "pct": round(v / total_exposure * 100, 1) if total_exposure > 0 else 0,
            }
            for k, v in sorted(theme_exposure.items(), key=lambda x: -x[1])
        },
        "diversification_score": div_score,
    }


@app.get("/api/analytics/signal_performance", dependencies=[Depends(_require_auth)])
def get_analytics_signal_performance() -> dict:
    """Signal source performance stats from backtest or recent data."""
    try:
        from intelligence.backtester import Backtester
        bt = Backtester()
        report = bt.run(days=7)
        return {
            "by_source": report.by_source,
            "period_days": 7,
            "total_signals": report.total_signals,
        }
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: count signals per source from recent data
    sig_data = _read_json("intelligence_signals.json")
    signals = sig_data.get("signals", []) if isinstance(sig_data, dict) else []

    source_counts: dict[str, int] = defaultdict(int)
    for s in signals:
        source = s.get("source", "unknown")
        source_counts[source] += 1

    health_data = _read_json("intelligence_health.json")

    by_source = {}
    for source, count in source_counts.items():
        status = "active"
        if isinstance(health_data, dict):
            h = health_data.get(source, {})
            status = h.get("status", "unknown")
        by_source[source] = {
            "signals": count,
            "win_rate": 0,
            "avg_pnl": 0,
            "total_pnl": 0,
            "sharpe": 0,
            "status": status,
        }

    return {
        "by_source": by_source,
        "period_days": 1,
        "total_signals": len(signals),
        "message": "Collecting data — backtest available after signal history accumulates",
    }


# ===========================================================================
# Combined / Overview Endpoints
# ===========================================================================

@app.get("/api/combined/overview", dependencies=[Depends(_require_auth)])
def get_combined_overview() -> dict:
    """Portfolio overview — events agent."""
    from datetime import timedelta as td

    SGT = timezone(td(hours=8))
    now_sgt = datetime.now(SGT)
    today_start = now_sgt.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    bankroll_data = _read_json("bankroll.json")
    system_status = _read_json("system_status.json")

    # --- Events agent data ---
    events_positions = _read_json("events_positions.json").get("positions", [])
    events_trades_raw = _read_json("events_trades.json").get("trades", [])
    events_open = [p for p in events_positions if p.get("status") == "open"]
    events_closed = [p for p in events_positions if p.get("status") != "open" and _is_real_trade(p)]

    events_mode = "paper"
    if events_trades_raw:
        events_mode = events_trades_raw[-1].get("mode", "paper")

    events_total_pnl = sum(p.get("pnl", 0) or 0 for p in events_closed)
    events_total_trades = len(events_closed)
    events_wins = sum(1 for p in events_closed if (p.get("pnl") or 0) > 0)
    events_win_rate = round(events_wins / events_total_trades * 100, 1) if events_total_trades > 0 else 0.0

    # Events today P&L and trades (SGT)
    events_today_pnl = 0.0
    events_today_trades = 0
    for p in events_closed:
        exit_ts = _parse_ts(p.get("exit_time"))
        if exit_ts and exit_ts >= today_start:
            events_today_pnl += p.get("pnl", 0) or 0
            events_today_trades += 1

    events_last_scan = system_status.get("events_last_scan")

    total_exposure = sum(p.get("cost", 0) or 0 for p in events_open)

    return {
        "total_portfolio": round(bankroll_data.get("current_bankroll", 0), 2),
        "total_pnl": round(events_total_pnl, 2),
        "total_open_positions": len(events_open),
        "total_exposure": round(total_exposure, 2),
        "today_pnl": round(events_today_pnl, 2),
        "today_trades": events_today_trades,
        "events": {
            "mode": events_mode,
            "open_count": len(events_open),
            "today_pnl": round(events_today_pnl, 2),
            "today_trades": events_today_trades,
            "win_rate": events_win_rate,
            "total_trades": events_total_trades,
            "last_scan": events_last_scan,
        },
    }


@app.get("/api/combined/equity-curve", dependencies=[Depends(_require_auth)])
def get_combined_equity_curve() -> dict:
    """Daily cumulative P&L — grouped by SGT date."""
    from datetime import timedelta as td

    SGT = timezone(td(hours=8))
    bankroll_data = _read_json("bankroll.json")
    starting = bankroll_data.get("starting_bankroll", 242.11)

    # Events daily P&L
    events_positions = _read_json("events_positions.json").get("positions", [])
    events_closed = [p for p in events_positions if p.get("status") != "open" and _is_real_trade(p)]
    events_daily: dict[str, float] = defaultdict(float)
    for p in events_closed:
        exit_ts = _parse_ts(p.get("exit_time"))
        if exit_ts:
            day = exit_ts.astimezone(SGT).strftime("%Y-%m-%d")
            events_daily[day] += p.get("pnl", 0) or 0

    all_days = sorted(events_daily.keys())

    dates = []
    events_cumulative = []
    events_running = 0.0

    for day in all_days:
        events_running += events_daily.get(day, 0.0)
        dates.append(day)
        events_cumulative.append(round(events_running, 2))

    return {
        "dates": dates,
        "events_cumulative": events_cumulative,
        "starting_bankroll": starting,
    }


@app.get("/api/combined/heatmap", dependencies=[Depends(_require_auth)])
def get_combined_heatmap() -> dict:
    """90-day calendar heatmap: daily P&L and trade counts."""
    from datetime import timedelta as td

    SGT = timezone(td(hours=8))
    now_sgt = datetime.now(SGT)
    cutoff = now_sgt - td(days=90)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    # Collect daily stats from Events
    events_positions = _read_json("events_positions.json").get("positions", [])
    events_closed = [p for p in events_positions if p.get("status") != "open" and _is_real_trade(p)]

    events_day_pnl: dict[str, float] = defaultdict(float)
    events_day_trades: dict[str, int] = defaultdict(int)
    for p in events_closed:
        exit_ts = _parse_ts(p.get("exit_time"))
        if exit_ts:
            day = exit_ts.astimezone(SGT).strftime("%Y-%m-%d")
            if day >= cutoff_str:
                events_day_pnl[day] += p.get("pnl", 0) or 0
                events_day_trades[day] += 1

    all_days = sorted(events_day_pnl.keys())

    days_out = []
    for day in all_days:
        ev_pnl = events_day_pnl.get(day, 0.0)
        total_trades = events_day_trades.get(day, 0)
        days_out.append({
            "date": day,
            "pnl": round(ev_pnl, 2),
            "trades": total_trades,
            "events_pnl": round(ev_pnl, 2),
        })

    return {"days": days_out}


@app.get("/api/combined/activity-feed", dependencies=[Depends(_require_auth)])
def get_combined_activity_feed() -> dict:
    """Last 50 activities (trades placed / resolved), newest first."""
    activities: list[dict] = []

    # --- Events: derive from positions ---
    events_positions = _read_json("events_positions.json").get("positions", [])
    for p in events_positions:
        question = p.get("market_question") or ""
        short_q = question[:60] + ("..." if len(question) > 60 else "")
        pnl = p.get("pnl") or 0
        side = p.get("side", "YES")
        entry_price = p.get("entry_price", 0) or 0
        cost = p.get("cost", 0) or 0
        # Format price as cents
        price_cents = f"{entry_price * 100:.1f}¢"

        if p.get("status") != "open" and p.get("exit_time"):
            won = pnl > 0
            label = f"WIN +${pnl:.2f}" if won else f"LOSS ${pnl:.2f}"
            activities.append({
                "time": p["exit_time"],
                "agent": "events",
                "type": "win" if won else "loss",
                "description": f"{short_q} — {label}",
                "amount": round(abs(pnl), 2),
            })
        elif p.get("status") == "open" and p.get("entry_time"):
            activities.append({
                "time": p["entry_time"],
                "agent": "events",
                "type": "bet_placed",
                "description": f"{short_q} — {side} @ {price_cents}",
                "amount": round(cost, 2),
            })

    # Sort descending by time, limit 50
    activities.sort(key=lambda a: a["time"] or "", reverse=True)
    return {"activities": activities[:50]}


@app.get("/api/combined/odds-snapshot", dependencies=[Depends(_require_auth)])
def get_combined_odds_snapshot() -> dict:
    """Current odds being watched. Reads odds_snapshots.json."""
    data = _read_json("odds_snapshots.json")
    if not data:
        return {"snapshots": [], "last_updated": None}

    # Support both dict-with-snapshots and raw list formats
    if isinstance(data, list):
        snapshots = data
        last_updated = None
    else:
        snapshots = data.get("snapshots", [])
        last_updated = data.get("last_updated") or data.get("timestamp")

    return {"snapshots": snapshots, "last_updated": last_updated}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
