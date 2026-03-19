"""
research_agent.py - Automated research agent for Polymarket 5-min crypto trading.

This runs as a scheduled task and performs:
1. Market structure analysis - current volume, spreads, liquidity
2. Strategy performance review - win rates, edge decay detection
3. Competitor bot analysis - leaderboard tracking
4. Parameter optimization recommendations
5. Risk assessment updates

Outputs findings to /home/user/workspace/polymarket-bot/research/reports/
"""

import os
import json
import time
import httpx
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

RESEARCH_DIR = Path("/home/user/workspace/polymarket-bot/research/reports")
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def fetch_market_intelligence():
    """Gather current state of 5-min crypto markets."""
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "markets": {},
        "insights": [],
        "recommendations": [],
    }

    assets = ["BTC", "ETH", "SOL"]
    now = time.time()
    window_start = int(now // 300) * 300

    for asset in assets:
        slug = f"{asset.lower()}-updown-5m-{window_start}"
        try:
            with httpx.Client(timeout=10) as client:
                # Try to fetch current market
                resp = client.get(
                    f"{GAMMA_BASE}/markets",
                    params={"slug": slug, "limit": 1}
                )
                markets = resp.json() if resp.status_code == 200 else []

                if markets and len(markets) > 0:
                    m = markets[0]
                    market_data = {
                        "slug": slug,
                        "volume": m.get("volume", "0"),
                        "volume_24h": m.get("volume_24hr", "0"),
                        "liquidity": m.get("liquidity", "0"),
                        "active": m.get("active", False),
                    }

                    # Get orderbook if we have token IDs
                    clob_ids = m.get("clobTokenIds", "")
                    if clob_ids:
                        token_ids = json.loads(clob_ids) if isinstance(clob_ids, str) else clob_ids
                        if token_ids:
                            try:
                                book_resp = client.get(
                                    f"{CLOB_BASE}/book",
                                    params={"token_id": token_ids[0]}
                                )
                                if book_resp.status_code == 200:
                                    book = book_resp.json()
                                    bids = book.get("bids", [])
                                    asks = book.get("asks", [])
                                    market_data["bid_levels"] = len(bids)
                                    market_data["ask_levels"] = len(asks)
                                    if bids and asks:
                                        best_bid = float(bids[0]["price"])
                                        best_ask = float(asks[0]["price"])
                                        market_data["spread"] = best_ask - best_bid
                                        market_data["midpoint"] = (best_bid + best_ask) / 2
                                        market_data["bid_depth"] = sum(float(b["size"]) for b in bids[:5])
                                        market_data["ask_depth"] = sum(float(a["size"]) for a in asks[:5])
                            except Exception as e:
                                logger.debug("Could not fetch orderbook for %s: %s", asset, e)

                    report["markets"][asset] = market_data
                else:
                    report["markets"][asset] = {"slug": slug, "status": "not_found"}

        except Exception as e:
            logger.warning("Error fetching market data for %s: %s", asset, e)
            report["markets"][asset] = {"error": str(e)}

    return report


def analyze_performance(report):
    """Analyze bot performance from trade logs."""
    log_file = Path("/home/user/workspace/polymarket-bot/data/trades.csv")
    stats_file = Path("/home/user/workspace/polymarket-bot/data/stats.json")

    if stats_file.exists():
        try:
            stats = json.loads(stats_file.read_text())
            report["performance"] = {
                "total_trades": stats.get("total_trades", 0),
                "win_rate": stats.get("win_rate", 0),
                "total_pnl": stats.get("total_pnl", 0),
                "sharpe_estimate": stats.get("sharpe_estimate", 0),
                "max_drawdown": stats.get("max_drawdown", 0),
                "best_trade": stats.get("best_trade", 0),
                "worst_trade": stats.get("worst_trade", 0),
            }

            # Edge decay detection
            if stats.get("total_trades", 0) > 20:
                recent_wr = stats.get("recent_win_rate", stats.get("win_rate", 0.5))
                overall_wr = stats.get("win_rate", 0.5)
                if recent_wr < overall_wr - 0.05:
                    report["insights"].append(
                        f"EDGE DECAY DETECTED: Recent win rate ({recent_wr:.1%}) is "
                        f"below overall ({overall_wr:.1%}). Consider pausing or adjusting."
                    )
                    report["recommendations"].append(
                        "Increase MIN_EDGE_THRESHOLD by 0.005 to filter out weaker signals"
                    )

        except Exception as e:
            report["performance"] = {"error": str(e)}
    else:
        report["performance"] = {"status": "no_data_yet"}

    return report


def generate_recommendations(report):
    """Generate actionable recommendations based on market conditions."""
    for asset, data in report.get("markets", {}).items():
        if isinstance(data, dict):
            spread = data.get("spread", 0)
            if spread and spread > 0.10:
                report["recommendations"].append(
                    f"{asset}: Wide spread ({spread:.2f}) — consider market-making "
                    f"or waiting for tighter spreads before trading"
                )
            elif spread and spread < 0.02:
                report["recommendations"].append(
                    f"{asset}: Very tight spread ({spread:.2f}) — good conditions "
                    f"for latency arbitrage"
                )

            depth = data.get("bid_depth", 0)
            if depth and depth < 100:
                report["recommendations"].append(
                    f"{asset}: Low liquidity (depth={depth:.0f}) — reduce position sizes"
                )

    return report


def save_report(report):
    """Save research report to file."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filepath = RESEARCH_DIR / f"research_{ts}.json"
    filepath.write_text(json.dumps(report, indent=2, default=str))
    return filepath


def run_research():
    """Main research function — called by cron."""
    logger.info("Running research agent...")

    report = fetch_market_intelligence()
    report = analyze_performance(report)
    report = generate_recommendations(report)

    filepath = save_report(report)
    logger.info("Research report saved: %s", filepath)

    # Build summary for notification
    summary_lines = [
        f"📊 Polymarket Research Report",
        f"Time: {report['timestamp']}",
        "",
    ]

    for asset, data in report.get("markets", {}).items():
        if isinstance(data, dict) and "midpoint" in data:
            summary_lines.append(
                f"{asset}: mid={data['midpoint']:.2f}, "
                f"spread={data.get('spread', 'N/A')}, "
                f"depth={data.get('bid_depth', 'N/A')}"
            )

    if report.get("insights"):
        summary_lines.append("")
        summary_lines.append("⚠️ Insights:")
        for insight in report["insights"]:
            summary_lines.append(f"  • {insight}")

    if report.get("recommendations"):
        summary_lines.append("")
        summary_lines.append("💡 Recommendations:")
        for rec in report["recommendations"]:
            summary_lines.append(f"  • {rec}")

    perf = report.get("performance", {})
    if perf.get("total_trades", 0) > 0:
        summary_lines.append("")
        summary_lines.append(
            f"📈 Performance: {perf.get('total_trades')} trades, "
            f"WR={perf.get('win_rate', 0):.1%}, "
            f"PnL=${perf.get('total_pnl', 0):.2f}"
        )

    return "\n".join(summary_lines), filepath


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    summary, path = run_research()
    print(summary)
    print(f"\nReport saved to: {path}")
