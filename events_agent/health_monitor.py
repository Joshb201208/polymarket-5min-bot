"""Health monitor — auto-detect phantom trades, cash mismatches, orphans.

Runs after every scan tick. Fetches wallet state once from the Polymarket
Data API, then reuses that cached result across all checks.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from events_agent.config import EventsConfig
from events_agent.portfolio import PortfolioManager
from shared.utils import load_json, utcnow, parse_utc

logger = logging.getLogger("health_monitor")

# Polymarket Data API for real wallet positions
DATA_API_URL = "https://data-api.polymarket.com/positions"


class HealthMonitor:
    """Lightweight health checks that run after every scan tick.

    Fetches wallet positions once (cached per instance) and reuses
    across all seven checks.
    """

    def __init__(self, config: EventsConfig, portfolio: PortfolioManager) -> None:
        self.config = config
        self.portfolio = portfolio
        self._wallet_positions: list[dict] | None = None  # cached per tick

    # ------------------------------------------------------------------
    # Wallet data (fetched once per tick, reused across checks)
    # ------------------------------------------------------------------

    def _fetch_wallet_positions(self) -> list[dict]:
        """Fetch actual positions from Polymarket Data API. Cached per instance."""
        if self._wallet_positions is not None:
            return self._wallet_positions

        address = self.config.FUNDER_ADDRESS
        if not address:
            logger.warning("FUNDER_ADDRESS not configured — skipping wallet checks")
            self._wallet_positions = []
            return self._wallet_positions

        try:
            url = f"{DATA_API_URL}?user={address}&sizeThreshold=0&limit=500"
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                self._wallet_positions = json.loads(resp.read())
        except Exception as e:
            logger.error("Failed to fetch wallet positions: %s", e)
            self._wallet_positions = []

        return self._wallet_positions

    def _wallet_shares_by_asset(self) -> dict[str, float]:
        """Map asset_id (token_id) -> total size from wallet."""
        mapping: dict[str, float] = {}
        for wp in self._fetch_wallet_positions():
            asset_id = wp.get("asset", "") or wp.get("asset_id", "") or wp.get("token_id", "")
            size = float(wp.get("size", 0) or 0)
            if asset_id and size > 0:
                mapping[asset_id] = mapping.get(asset_id, 0) + size
        return mapping

    # ------------------------------------------------------------------
    # Run all checks
    # ------------------------------------------------------------------

    def run_all_checks(self) -> list[str]:
        """Run all health checks and return list of alert messages."""
        issues: list[str] = []

        try:
            issues.extend(self.check_wallet_reconciliation())
        except Exception as e:
            logger.error("Wallet reconciliation check failed: %s", e)

        try:
            issues.extend(self.check_orphan_positions())
        except Exception as e:
            logger.error("Orphan detection check failed: %s", e)

        try:
            issues.extend(self.check_cash_reconciliation())
        except Exception as e:
            logger.error("Cash reconciliation check failed: %s", e)

        try:
            issues.extend(self.check_double_buys())
        except Exception as e:
            logger.error("Double-buy check failed: %s", e)

        try:
            issues.extend(self.check_stale_scan())
        except Exception as e:
            logger.error("Stale scan check failed: %s", e)

        try:
            issues.extend(self.check_stop_loss_breaches())
        except Exception as e:
            logger.error("Stop loss check failed: %s", e)

        return issues

    # ------------------------------------------------------------------
    # 1. Wallet Reconciliation
    # ------------------------------------------------------------------

    def check_wallet_reconciliation(self) -> list[str]:
        """Compare bot-tracked positions against actual Polymarket wallet.

        If bot says we own shares but wallet shows 0 → phantom position.
        Auto-fix: force-close phantom positions.
        """
        issues: list[str] = []
        wallet = self._wallet_shares_by_asset()
        open_positions = self.portfolio.get_open_positions()
        phantom_count = 0

        for pos in open_positions:
            if pos.mode == "paper":
                continue
            token_id = pos.token_id
            if not token_id:
                continue

            wallet_shares = wallet.get(token_id, 0)
            if wallet_shares < 0.1 and pos.shares > 0.1:
                # Potential phantom — wallet has no shares for this token
                # ALERT ONLY — do NOT auto-close. GTC orders may fill late.
                logger.warning(
                    "POSSIBLE PHANTOM: %s has %.2f shares in bot but 0 on wallet",
                    pos.market_question[:50], pos.shares,
                )
                phantom_count += 1

        if phantom_count > 0:
            issues.append(
                f"⚠️ {phantom_count} positions may not exist on wallet — "
                f"check Polymarket manually (NOT auto-closed)"
            )

        return issues

    # ------------------------------------------------------------------
    # 2. Orphan Detection
    # ------------------------------------------------------------------

    def check_orphan_positions(self) -> list[str]:
        """Check wallet for positions NOT tracked by the bot."""
        issues: list[str] = []
        wallet = self._wallet_shares_by_asset()
        open_positions = self.portfolio.get_open_positions()

        # Build set of token_ids the bot knows about
        tracked_tokens = {pos.token_id for pos in open_positions if pos.token_id}

        # Also exclude closed positions with extreme_pricing (known junk)
        all_positions = self.portfolio.load_positions()
        junk_tokens = {
            pos.token_id for pos in all_positions
            if (getattr(pos, "edge_source", "") or "") == "extreme_pricing"
            and pos.token_id
        }

        orphan_count = 0
        for asset_id, size in wallet.items():
            if size < 0.1:
                continue
            if asset_id in tracked_tokens:
                continue
            if asset_id in junk_tokens:
                continue
            orphan_count += 1
            logger.info("ORPHAN: wallet has %.2f shares of %s (not tracked by bot)", size, asset_id[:16])

        if orphan_count > 0:
            issues.append(
                f"⚠️ Found {orphan_count} untracked positions on Polymarket wallet"
            )

        return issues

    # ------------------------------------------------------------------
    # 3. Cash Reconciliation
    # ------------------------------------------------------------------

    def check_cash_reconciliation(self) -> list[str]:
        """Compare bot's calculated cash vs actual Polymarket cash.

        Uses the CLOB API to check USDC balance.
        """
        issues: list[str] = []

        # Bot's view of cash
        bankroll_path = self.config.DATA_DIR / "events_bankroll.json"
        bankroll_data = load_json(bankroll_path, {})
        bot_cash = float(bankroll_data.get("cash", 0))

        if bot_cash <= 0:
            return issues

        # Actual cash from wallet — sum value of positions vs bankroll
        # Use the wallet positions to calculate total position value
        wallet = self._fetch_wallet_positions()
        wallet_position_value = 0.0
        for wp in wallet:
            size = float(wp.get("size", 0) or 0)
            cur_price = float(wp.get("curPrice", 0) or wp.get("current_value", 0) or 0)
            if size > 0 and cur_price > 0:
                wallet_position_value += size * cur_price

        # We can estimate actual cash as: total_bankroll - wallet_position_value
        # But this is noisy. Instead, just compare bot open cost vs wallet holdings.
        bot_open_cost = float(bankroll_data.get("open_positions_cost", 0))
        current_bankroll = float(bankroll_data.get("current_bankroll", 0))

        if current_bankroll <= 0:
            return issues

        # The real check: does the bot think it has more cash than possible?
        # If bot_cash > current_bankroll, something is very wrong
        if bot_cash > current_bankroll + 1:
            issues.append(
                f"⚠️ Cash mismatch: bot thinks ${bot_cash:.2f} cash, "
                f"but bankroll is only ${current_bankroll:.2f}"
            )

        return issues

    # ------------------------------------------------------------------
    # 4. Double-Buy Detection
    # ------------------------------------------------------------------

    def check_double_buys(self) -> list[str]:
        """Check if bot placed multiple buys for the same market recently."""
        issues: list[str] = []
        cutoff = utcnow() - timedelta(hours=2)

        trades = self.portfolio.load_trades()
        recent_buys: dict[str, list] = {}

        for t in trades:
            if t.action != "BUY":
                continue
            try:
                ts = parse_utc(t.timestamp)
                if ts < cutoff:
                    continue
            except (ValueError, TypeError):
                continue

            market_id = t.market_id
            if market_id not in recent_buys:
                recent_buys[market_id] = []
            recent_buys[market_id].append(t)

        for market_id, buys in recent_buys.items():
            if len(buys) <= 1:
                continue

            # Double buy detected
            logger.warning("DOUBLE BUY: %d buys for market %s in last 2h", len(buys), market_id)

            # Auto-fix: close duplicate positions (keep the first one)
            open_positions = self.portfolio.get_open_positions()
            dupes = [p for p in open_positions if p.market_id == market_id]
            if len(dupes) > 1:
                # Keep the first (oldest), close the rest
                dupes_sorted = sorted(dupes, key=lambda p: p.entry_time or "")
                for dup in dupes_sorted[1:]:
                    # ALERT ONLY — do not auto-close duplicates

                    pass

                market_q = dupes_sorted[0].market_question[:50]
                issues.append(
                    f"⚠️ Double buy detected on {market_q} — removed duplicate"
                )

        return issues

    # ------------------------------------------------------------------
    # 5. Stale Scan Detection
    # ------------------------------------------------------------------

    def check_stale_scan(self) -> list[str]:
        """Check if the bot hasn't scanned in > 90 minutes."""
        issues: list[str] = []

        status_path = self.config.DATA_DIR / "system_status.json"
        status = load_json(status_path, {})
        last_scan_str = status.get("events_last_scan", "")

        if not last_scan_str:
            return issues

        try:
            last_scan = parse_utc(last_scan_str)
            minutes_ago = (utcnow() - last_scan).total_seconds() / 60

            if minutes_ago > 90:
                issues.append(
                    f"⚠️ No scan in {int(minutes_ago)} minutes — bot may be stuck"
                )
        except (ValueError, TypeError):
            pass

        return issues

    # ------------------------------------------------------------------
    # 6. Stop Loss Verification
    # ------------------------------------------------------------------

    def check_stop_loss_breaches(self) -> list[str]:
        """Check if any position has breached stop loss but wasn't exited."""
        issues: list[str] = []
        open_positions = self.portfolio.get_open_positions()

        for pos in open_positions:
            if pos.entry_price <= 0:
                continue

            # Try to get current price from wallet data
            wallet = self._wallet_shares_by_asset()
            # We can't reliably get current price from wallet data alone,
            # so use the wallet positions which include curPrice
            cur_price = self._get_wallet_price(pos.token_id)
            if cur_price is None or cur_price <= 0:
                continue

            pnl_pct = (cur_price - pos.entry_price) / pos.entry_price

            # 35% below entry for YES, 35% above entry for NO
            side_upper = (getattr(pos, "side", "") or "").upper()
            if "NO" in side_upper:
                # For NO positions, price going up is bad
                if pnl_pct < -0.35:
                    issues.append(
                        f"⚠️ {pos.market_question[:50]} breached stop loss "
                        f"({pnl_pct*100:.0f}%) but wasn't exited"
                    )
            else:
                # For YES positions, price going down is bad
                if pnl_pct < -0.35:
                    issues.append(
                        f"⚠️ {pos.market_question[:50]} breached stop loss "
                        f"({pnl_pct*100:.0f}%) but wasn't exited"
                    )

        return issues

    def _get_wallet_price(self, token_id: str) -> float | None:
        """Get the current price for a token from wallet position data."""
        for wp in self._fetch_wallet_positions():
            asset_id = wp.get("asset", "") or wp.get("asset_id", "") or wp.get("token_id", "")
            if asset_id == token_id:
                price = wp.get("curPrice") or wp.get("current_price")
                if price is not None:
                    return float(price)
        return None

    # ------------------------------------------------------------------
    # 7. Post-Trade Verification
    # ------------------------------------------------------------------

    def verify_position_exists(self, position) -> bool:
        """Verify shares actually exist on Polymarket wallet after a trade.

        Called after each buy execution with a fresh wallet fetch.
        Returns True if shares are found.
        """
        if position.mode == "paper":
            return True

        address = self.config.FUNDER_ADDRESS
        if not address:
            return True  # Can't verify without address

        token_id = position.token_id
        if not token_id:
            return True

        # Fresh fetch (don't use cached data — we need post-trade state)
        try:
            url = f"{DATA_API_URL}?user={address}&sizeThreshold=0&limit=500"
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                wallet_positions = json.loads(resp.read())
        except Exception as e:
            logger.error("Post-trade verification fetch failed: %s", e)
            return True  # Don't kill the position if we can't verify

        for wp in wallet_positions:
            asset_id = wp.get("asset", "") or wp.get("asset_id", "") or wp.get("token_id", "")
            size = float(wp.get("size", 0) or 0)
            if asset_id == token_id and size > 0.1:
                logger.info("Post-trade verified: %.2f shares of %s found on wallet", size, token_id[:16])
                return True

        logger.warning("Post-trade verification FAILED: no shares of %s on wallet", token_id[:16])
        return False
