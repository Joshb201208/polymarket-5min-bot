"""One-time cleanup: sell all extreme_pricing junk positions on Polymarket.

Scans the wallet for ALL open positions, identifies ones NOT tracked by the bot
(these are the extreme_pricing junk), and sells them via GTC limit orders.

Uses CLOB client.get_positions() as primary method (authenticated, complete),
with Data API pagination as fallback.

Usage: python -m events_agent.cleanup_extreme
"""

import json
import logging
import time
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cleanup")


def get_positions_from_clob(client) -> list[dict]:
    """Primary method: use authenticated CLOB client to get all positions."""
    try:
        all_positions = []  # VPS py-clob-client version lacks get_positions()
        logger.info("CLOB returned %d positions", len(all_positions))

        wallet_positions = []
        for pos in all_positions:
            # asset can be a dict with token_id or a string
            asset = pos.get("asset", {})
            token_id = asset.get("token_id", "") if isinstance(asset, dict) else str(asset)
            size = float(pos.get("size", 0))
            if size > 0.01:  # Skip dust
                wallet_positions.append({
                    "token_id": token_id,
                    "shares": size,
                    "avg_price": float(pos.get("avgPrice", 0)),
                })
        return wallet_positions
    except Exception as e:
        logger.error("CLOB get_positions() failed: %s", e)
        return []


def get_positions_from_data_api(address: str) -> list[dict]:
    """Fallback: paginate through Data API to get all positions."""
    wallet_positions = []
    offset = 0
    limit = 500
    while True:
        try:
            url = (
                f"https://data-api.polymarket.com/positions"
                f"?user={address}&limit={limit}&offset={offset}&sizeThreshold=0"
            )
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                batch = json.loads(resp.read())

            if not isinstance(batch, list):
                break

            for pos in batch:
                token_id = pos.get("asset", "")
                size = float(pos.get("size", 0))
                if size > 0.01:
                    wallet_positions.append({
                        "token_id": token_id,
                        "shares": size,
                        "avg_price": float(pos.get("curPrice", 0)),
                    })

            if len(batch) < limit:
                break
            offset += limit

        except Exception as e:
            logger.error("Data API failed at offset %d: %s", offset, e)
            break

    logger.info("Data API returned %d positions (after dust filter)", len(wallet_positions))
    return wallet_positions


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


def get_market_question(token_id: str) -> tuple[str, bool]:
    """Fetch market question and neg_risk from Gamma API."""
    try:
        url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            markets = json.loads(resp.read())
            if markets and len(markets) > 0:
                question = markets[0].get("question", token_id[:20])
                neg_risk = markets[0].get("neg_risk", False)
                return question, neg_risk
    except Exception:
        pass
    return token_id[:20], False


def sell_position_gtc(client, token_id: str, shares: float, market_question: str, neg_risk: bool) -> str:
    """Sell a position using GTC limit order at slightly below midpoint."""
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import SELL

    try:
        # Get tick size and midpoint
        tick_size = str(client.get_tick_size(token_id))

        try:
            mid_data = client.get_midpoint(token_id)
            midpoint = float(mid_data.get("mid", 0)) if isinstance(mid_data, dict) else float(mid_data)
        except Exception as e:
            logger.warning("SKIP (midpoint error, likely resolved market): %s | %s",
                           market_question[:50], e)
            return ""

        if midpoint <= 0.01:
            logger.warning("SKIP: midpoint too low (%.4f) for %s", midpoint, market_question[:50])
            return ""

        # Skip tiny-value positions (not worth the effort)
        position_value = shares * midpoint
        if position_value < 0.50:
            logger.info("SKIP: value too low ($%.2f) for %s", position_value, market_question[:50])
            return ""

        # Sell at 95% of midpoint for faster fill on junk positions
        decimals = len(tick_size.split('.')[-1]) if '.' in tick_size else 2
        sell_price = round(midpoint * 0.95, decimals)
        sell_price = max(sell_price, 0.01)

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

    # Step 2: Get all wallet positions (Data API primary — CLOB has auth issues from VPS)
    all_positions = get_positions_from_data_api(config.FUNDER_ADDRESS)
    if not all_positions:
        logger.warning("Data API returned no positions, trying CLOB fallback...")
        all_positions = get_positions_from_clob(client)

    if not all_positions:
        logger.info("No positions found in wallet")
        return

    logger.info("Total positions found: %d", len(all_positions))

    # Step 3: Get bot-tracked positions
    bot_token_ids = get_bot_tracked_token_ids()
    logger.info("Bot tracks %d open positions", len(bot_token_ids))

    # Step 4: Identify junk (positions NOT tracked by bot)
    junk = []
    for pos in all_positions:
        token_id = pos["token_id"]
        if token_id and token_id not in bot_token_ids:
            junk.append(pos)

    logger.info("Found %d junk positions to sell", len(junk))

    if not junk:
        logger.info("No junk positions found — wallet is clean")
        return

    # Step 5: Sell all junk positions
    sold = 0
    failed = 0
    skipped = 0

    for j in junk:
        token_id = j["token_id"]

        # Get market info for logging and neg_risk
        question, neg_risk = get_market_question(token_id)

        order_id = sell_position_gtc(
            client,
            token_id,
            j["shares"],
            question,
            neg_risk,
        )

        if order_id:
            sold += 1
        elif order_id == "":
            # Empty string means skipped or failed
            skipped += 1
        else:
            failed += 1

        # Rate limit: don't spam the API
        time.sleep(1)

    logger.info("Cleanup complete: %d sold, %d skipped, %d failed",
                sold, skipped, failed)
    logger.info("GTC orders will fill as buyers match. Check wallet in 1-2 hours.")


if __name__ == "__main__":
    main()
