#!/usr/bin/env python3
"""Read-only Alpaca account report for the audit.

Sends GET requests only. It cannot place, change, or cancel orders: the single
function that talks to the network (`_get`) issues nothing but GET requests,
and nothing else in this file opens a connection.

Standard library only, so it runs with any Python 3.8+ on the droplet without
installing anything.

Usage (normally called by scripts/vps_snapshot.sh):

    python3 scripts/alpaca_readonly_report.py --env-file .env --out /tmp/alpaca

Reads ALPACA_API_KEY, ALPACA_SECRET_KEY and ALPACA_BASE_URL from the env file
(or from the environment). Writes JSON files plus summary.md into --out.

The output contains private account data. Never commit it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE_URL = "https://paper-api.alpaca.markets"
TIMEOUT_SECONDS = 30
MAX_PAGES = 60
REDACT_ACCOUNT_FIELDS = ("account_number", "id")


def load_env_file(path: str | None) -> dict[str, str]:
    """Parse simple KEY=VALUE lines. Does not print or log anything."""
    values: dict[str, str] = {}
    if not path:
        return values
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"Could not read env file: {exc.strerror}", file=sys.stderr)
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


class Alpaca:
    def __init__(self, base_url: str, key: str, secret: str):
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        }
        self.errors: list[str] = []

    def _get(self, path: str, params: dict | None = None):
        """The only network call in this script. GET, nothing else."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=self._headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.errors.append(f"GET {path}: HTTP {exc.code}")
        except Exception as exc:  # network error, bad JSON
            self.errors.append(f"GET {path}: {type(exc).__name__}")
        return None

    def account(self):
        data = self._get("/v2/account")
        if isinstance(data, dict):
            for field in REDACT_ACCOUNT_FIELDS:
                if field in data:
                    data[field] = "REDACTED"
        return data

    def positions(self):
        data = self._get("/v2/positions")
        return data if isinstance(data, list) else None

    def clock(self):
        return self._get("/v2/clock")

    def portfolio_history(self):
        return self._get(
            "/v2/account/portfolio/history", {"period": "1A", "timeframe": "1D"}
        )

    def orders(self) -> list[dict] | None:
        """All orders, newest first, paged backwards with `until`."""
        collected: list[dict] = []
        seen: set[str] = set()
        until = None
        for _ in range(MAX_PAGES):
            params = {
                "status": "all",
                "limit": 500,
                "direction": "desc",
                "nested": "true",
            }
            if until:
                params["until"] = until
            page = self._get("/v2/orders", params)
            if not isinstance(page, list):
                return collected if collected else None
            fresh = [o for o in page if o.get("id") not in seen]
            if not fresh:
                break
            for order in fresh:
                seen.add(order.get("id"))
            collected.extend(fresh)
            if len(page) < 500:
                break
            until = page[-1].get("submitted_at")
            if not until:
                break
        return collected

    def activities(self) -> list[dict] | None:
        """Account activities (fills, dividends, fees...), paged with page_token."""
        collected: list[dict] = []
        seen: set[str] = set()
        token = None
        for _ in range(MAX_PAGES):
            params = {"page_size": 100, "direction": "desc"}
            if token:
                params["page_token"] = token
            page = self._get("/v2/account/activities", params)
            if not isinstance(page, list):
                return collected if collected else None
            fresh = [a for a in page if a.get("id") not in seen]
            if not fresh:
                break
            for activity in fresh:
                seen.add(activity.get("id"))
            collected.extend(fresh)
            if len(page) < 100:
                break
            token = page[-1].get("id")
            if not token:
                break
        return collected


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_summary(base_url, account, positions, orders, activities, history, clock, errors) -> str:
    lines = ["# Alpaca read-only report", ""]
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    is_paper = "paper" in base_url
    lines.append(f"Endpoint: {'paper' if is_paper else 'NOT PAPER (check ALPACA_BASE_URL)'}")
    lines.append("")

    if errors:
        lines.append("## Requests that failed")
        lines.extend(f"- {e}" for e in errors)
        lines.append("")

    if isinstance(account, dict):
        equity = _float(account.get("equity"))
        lines.append("## Account")
        for field in (
            "status", "equity", "last_equity", "cash", "buying_power",
            "long_market_value", "short_market_value", "options_buying_power",
            "options_approved_level", "options_trading_level", "pattern_day_trader",
            "daytrade_count", "trading_blocked", "account_blocked", "created_at",
        ):
            if field in account:
                lines.append(f"- {field}: {account[field]}")
        lines.append("")
    else:
        equity = 0.0
        lines += ["## Account", "- not available", ""]

    if isinstance(positions, list):
        lines.append(f"## Positions ({len(positions)})")
        by_class = Counter(p.get("asset_class", "unknown") for p in positions)
        lines.append("- by asset class: " + ", ".join(f"{k}={v}" for k, v in sorted(by_class.items())))
        ranked = sorted(positions, key=lambda p: abs(_float(p.get("market_value"))), reverse=True)
        if equity > 0:
            lines.append("")
            lines.append("| symbol | class | qty | weight of equity | unrealized P&L % |")
            lines.append("| --- | --- | --- | --- | --- |")
            for p in ranked:
                weight = _float(p.get("market_value")) / equity
                plpc = _float(p.get("unrealized_plpc"))
                lines.append(
                    f"| {p.get('symbol')} | {p.get('asset_class', '')} | {p.get('qty')} "
                    f"| {weight:.1%} | {plpc:.1%} |"
                )
        lines.append("")
    else:
        lines += ["## Positions", "- not available", ""]

    if isinstance(orders, list):
        lines.append(f"## Orders ({len(orders)} in history)")
        if orders:
            dates = sorted(o.get("submitted_at") or "" for o in orders)
            lines.append(f"- first: {dates[0][:10]}  last: {dates[-1][:10]}")
        status = Counter(o.get("status", "unknown") for o in orders)
        lines.append("- by status: " + ", ".join(f"{k}={v}" for k, v in status.most_common()))
        kinds = Counter(f"{o.get('side')}/{o.get('type')}" for o in orders)
        lines.append("- by side/type: " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))

        open_states = {"new", "accepted", "held", "partially_filled", "pending_new", "accepted_for_bidding"}
        open_orders = [o for o in orders if o.get("status") in open_states]
        lines.append(f"- open now: {len(open_orders)}")

        if isinstance(positions, list):
            held = {p.get("symbol"): _float(p.get("qty")) for p in positions}
            open_stops: dict[str, float] = {}
            stop_count: Counter = Counter()
            for o in open_orders:
                if o.get("side") == "sell" and o.get("type") in ("stop", "stop_limit", "trailing_stop"):
                    sym = o.get("symbol")
                    open_stops[sym] = open_stops.get(sym, 0.0) + _float(o.get("qty"))
                    stop_count[sym] += 1
            equity_syms = [
                p.get("symbol") for p in positions if p.get("asset_class", "us_equity") == "us_equity"
            ]
            orphans = sorted(s for s in open_stops if s not in held)
            unprotected = sorted(s for s in equity_syms if s not in open_stops)
            mismatched = sorted(
                s for s in equity_syms if s in open_stops and abs(open_stops[s] - held[s]) > 1e-9
            )
            duplicates = sorted(s for s, n in stop_count.items() if n > 1)
            lines.append("")
            lines.append("### Stop-order cross-check (open sell stops against equity positions)")
            lines.append(f"- stop orders with no position ({len(orphans)}): {', '.join(orphans) or 'none'}")
            lines.append(f"- positions with no stop order ({len(unprotected)}): {', '.join(unprotected) or 'none'}")
            lines.append(f"- stop quantity differs from position ({len(mismatched)}): {', '.join(mismatched) or 'none'}")
            lines.append(f"- more than one stop order ({len(duplicates)}): {', '.join(duplicates) or 'none'}")
        lines.append("")
    else:
        lines += ["## Orders", "- not available", ""]

    if isinstance(activities, list):
        lines.append(f"## Account activities ({len(activities)})")
        kinds = Counter(a.get("activity_type", "unknown") for a in activities)
        lines.append("- by type: " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))
        lines.append("")
    else:
        lines += ["## Account activities", "- not available", ""]

    if isinstance(history, dict) and history.get("equity"):
        points = [e for e in history["equity"] if e]
        if points:
            lines.append("## Portfolio history (daily, up to one year)")
            lines.append(f"- data points: {len(points)}")
            base = _float(history.get("base_value")) or points[0]
            if base:
                lines.append(f"- change over the period: {(points[-1] - base) / base:.2%}")
            peak, worst = points[0], 0.0
            for value in points:
                peak = max(peak, value)
                if peak > 0:
                    worst = min(worst, (value - peak) / peak)
            lines.append(f"- largest drop from a peak: {worst:.2%}")
            lines.append("")

    if isinstance(clock, dict):
        lines.append("## Market clock")
        lines.append(f"- is_open: {clock.get('is_open')}  next_open: {clock.get('next_open')}")
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Alpaca account report (GET requests only).")
    parser.add_argument("--env-file", help="Path to the bot's .env file")
    parser.add_argument("--out", required=True, help="Directory to write the report into")
    args = parser.parse_args()

    env = {**load_env_file(args.env_file)}
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL"):
        if os.environ.get(name) and name not in env:
            env[name] = os.environ[name]

    key = env.get("ALPACA_API_KEY", "")
    secret = env.get("ALPACA_SECRET_KEY", "")
    base_url = env.get("ALPACA_BASE_URL") or DEFAULT_BASE_URL

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if not key or not secret:
        (out / "summary.md").write_text(
            "# Alpaca read-only report\n\nALPACA_API_KEY or ALPACA_SECRET_KEY is not set. Nothing was fetched.\n"
        )
        print("Alpaca credentials not found; wrote a placeholder summary.", file=sys.stderr)
        return 1

    api = Alpaca(base_url, key, secret)
    results = {
        "account": api.account(),
        "positions": api.positions(),
        "orders": api.orders(),
        "activities": api.activities(),
        "portfolio_history": api.portfolio_history(),
        "clock": api.clock(),
    }
    for name, data in results.items():
        if data is not None:
            (out / f"{name}.json").write_text(json.dumps(data, indent=1))

    (out / "summary.md").write_text(
        build_summary(
            base_url,
            results["account"],
            results["positions"],
            results["orders"],
            results["activities"],
            results["portfolio_history"],
            results["clock"],
            api.errors,
        )
    )
    fetched = [name for name, data in results.items() if data is not None]
    print(f"Alpaca report written to {out} ({', '.join(fetched) or 'nothing fetched'}).")
    return 0 if not api.errors else 2


if __name__ == "__main__":
    sys.exit(main())
