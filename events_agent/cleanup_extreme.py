"""One-time cleanup: sell all extreme_pricing junk positions on Polymarket.

Scans the wallet for ALL open positions, identifies ones NOT tracked by the bot
(these are the extreme_pricing junk), and sells them via GTC limit orders.

Usage: python -m events_agent.cleanup_extreme
"""

import json
import logging
import time
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cleanup")


def get_wallet_positions(funder_address: str) -> list[dict]:
    """Fetch ALL positions from Polymarket for this wallet."""
    positions = []
    # Try the data API first
    try:
        url = f"https://data-api.polymarket.com/positions?user={funder_address}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            if isinstance(data, list):
                positions = data
            logger.info("Found %d positions from data API", len(positions))
    except Exception as e:
        logger.error("Data API failed: %s", e)

    if not positions:
        # Fallback: try gamma API
        try:
            url = f"https://gamma-api.polymarket.com/positions?user={funder_address}&limit=500"
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                if isinstance(data, list):
                    positions = data
                logger.info("Found %d positions from gamma API", len(positions))
        except Exception as e:
            logger.error("Gamma API failed: %s", e)

    return positions


def get_bot_tracked_token_ids() -> set[str]:
    """Get token IDs of positions the bot is actively tracking."""
    data_dir = Path("/root/polymarket-bot/data")
    positions_file = data_dir / "events_positions.json"

    if not positions_file.exists():
        return set()

    try:
        data = json.loads(positions_file.read_text())
        positions = data.get("positions", [])
        # Get token IDs of open positions
        return {p.get("token_id", "") for p in positions if p.get("status") == "open"}
    except Exception:
        return set()


def sell_position_gtc(client, token_id: str, shares: float, market_question: str) -> str:
    """Sell a position using GTC limit order at slightly below midpoint."""
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import SELL

    try:
        # Get tick size and midpoint
        tick_size = str(client.get_tick_size(token_id))
        mid_data = client.get_midpoint(token_id)
        midpoint = float(mid_data.get("mid", 0)) if isinstance(mid_data, dict) else float(mid_data)

        if midpoint <= 0.01:
            logger.warning("SKIP: midpoint too low (%.4f) for %s", midpoint, market_question[:50])
            return ""

        # Sell at 95% of midpoint for faster fill on junk positions
        decimals = len(tick_size.split('.')[-1]) if '.' in tick_size else 2
        sell_price = round(midpoint * 0.95, decimals)
        sell_price = max(sell_price, 0.01)

        # Check neg_risk
        neg_risk = False
        try:
            url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                markets = json.loads(resp.read())
                if markets and len(markets) > 0:
                    neg_risk = markets[0].get("neg_risk", False)
        except Exception:
            pass

        order_args = OrderArgs(
            token_id=token_id,
            price=sell_price,
            size=round(shares, 2),
            side=SELL,
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk if neg_risk else None,
        )
        signed = client.create_order(order_args, options)
        resp = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID", "") if isinstance(resp, dict) else str(resp)

        if order_id:
            logger.info("GTC SELL posted: %s shares=%.2f @ %.3f | %s",
                        order_id[:12], shares, sell_price, market_question[:50])
        return order_id

    except Exception as e:
        logger.error("SELL failed for %s: %s", market_question[:40], e)
        return ""


def main():
    """Main cleanup flow."""
    from events_agent.config import EventsConfig
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    config = EventsConfig()

    if not config.FUNDER_ADDRESS:
        logger.error("No FUNDER_ADDRESS configured")
        return

    logger.info("Starting extreme_pricing cleanup for wallet: %s", config.FUNDER_ADDRESS[:10])

    # Step 1: Initialize CLOB client
    client = ClobClient(
        config.CLOB_API_BASE,
        key=config.PRIVATE_KEY,
        chain_id=137,
        signature_type=1,
        funder=config.FUNDER_ADDRESS,
    )
    if config.POLYMARKET_API_KEY and config.POLYMARKET_API_SECRET and config.POLYMARKET_API_PASSPHRASE:
        client.set_api_creds(ApiCreds(
            api_key=config.POLYMARKET_API_KEY,
            api_secret=config.POLYMARKET_API_SECRET,
            api_passphrase=config.POLYMARKET_API_PASSPHRASE,
        ))
    else:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

    logger.info("CLOB client initialized")

    # Step 2: Get all wallet positions
    all_positions = get_wallet_positions(config.FUNDER_ADDRESS)
    if not all_positions:
        logger.info("No positions found in wallet")
        return

    # Step 3: Get bot-tracked positions
    bot_token_ids = get_bot_tracked_token_ids()
    logger.info("Bot tracks %d open positions", len(bot_token_ids))

    # Step 4: Identify junk (positions NOT tracked by bot)
    junk = []
    for pos in all_positions:
        token_id = pos.get("token_id") or pos.get("asset") or ""
        if token_id and token_id not in bot_token_ids:
            shares = float(pos.get("size", 0) or pos.get("shares", 0) or 0)
            if shares > 0:
                market_q = pos.get("market", {}).get("question", "") or pos.get("title", "") or token_id[:20]
                current_price = float(pos.get("current_price", 0) or pos.get("price", 0) or 0)
                value = shares * current_price if current_price > 0 else 0
                junk.append({
                    "token_id": token_id,
                    "shares": shares,
                    "question": market_q,
                    "price": current_price,
                    "value": value,
                })

    logger.info("Found %d junk positions to sell (total value ~$%.2f)",
                len(junk), sum(j["value"] for j in junk))

    if not junk:
        logger.info("No junk positions found — wallet is clean")
        return

    # Step 5: Sell all junk positions
    sold = 0
    failed = 0
    total_value = 0

    for j in junk:
        if j["shares"] < 0.01:
            continue

        order_id = sell_position_gtc(
            client,
            j["token_id"],
            j["shares"],
            j["question"],
        )

        if order_id:
            sold += 1
            total_value += j["value"]
        else:
            failed += 1

        # Rate limit: don't spam the API
        time.sleep(1)

    logger.info("Cleanup complete: %d sold, %d failed, ~$%.2f in GTC sell orders posted",
                sold, failed, total_value)
    logger.info("GTC orders will fill as buyers match. Check wallet in 1-2 hours.")


if __name__ == "__main__":
    main()
