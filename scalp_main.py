"""
scalp_main.py - Main orchestrator for the Polymarket scalp trading bot.

Runs a fast 2-second loop that:
  1. Checks exits FIRST (always close before opening)
  2. Checks for new entry signals
  3. Executes trades via paper or live mode

This is a SEPARATE entry point from main.py. Switch via the systemd
service file or run directly:

    python scalp_main.py
    TRADING_MODE=paper python scalp_main.py

v1.0 — Initial scalp bot release
"""

import os
import sys
import time
import signal
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import httpx

from config import load_config, Config
from data_feeds import PriceFeed
from market_finder import MarketFinder
from scalp_strategy import ScalpStrategy, ScalpSignal, ExitSignal, ScalpPosition
from scalp_paper_trader import ScalpPaperTrader
from executor import OrderExecutor
from utils import (
    format_usd,
    format_pct,
    seconds_until_window_end,
    current_window,
    send_telegram_message,
    round_to_tick,
)

logger = logging.getLogger("scalp_main")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLOB_BASE = "https://clob.polymarket.com"


# ---------------------------------------------------------------------------
# ScalpBot
# ---------------------------------------------------------------------------

class ScalpBot:
    """
    Orchestrates the scalp trading strategy with a fast 2-second loop.
    """

    STARTUP_WAIT = 20       # seconds to wait for price feed
    WATCHDOG_TIMEOUT = 120  # seconds before watchdog alerts
    HEALTH_REPORT_INTERVAL = 3600  # seconds between health reports (1 hour)

    def __init__(self, config: Config):
        self._config = config
        self._running = False
        self._shutdown_event = threading.Event()
        self._last_loop_ts: float = time.time()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._start_time = time.time()
        self._cycle_count = 0
        self._last_health_report = 0.0

        # Load scalp config (uses ScalpConfig from config.py if available)
        sc = getattr(config, "scalp", None)
        self._loop_interval = getattr(sc, "loop_interval", 2.0) if sc else 2.0

        logger.info("=" * 60)
        logger.info("Polymarket SCALP Bot — %s mode", config.trading_mode.upper())
        logger.info("Loop interval: %.1fs", self._loop_interval)
        logger.info("=" * 60)

        # Core modules
        self._price_feed = PriceFeed(
            assets=config.strategy.assets,
            history_minutes=config.exchange.price_history_minutes,
        )
        if config.telegram and config.telegram.is_configured:
            self._price_feed.configure_alerts(
                bot_token=config.telegram.bot_token,
                chat_id=config.telegram.chat_id,
            )

        self._market_finder = MarketFinder(
            assets=config.strategy.assets,
            gamma_base=config.api.gamma_base,
        )

        # Build ScalpStrategy from config
        self._strategy = self._build_strategy(config)

        # Paper or live mode
        if config.is_paper_mode:
            self._paper_trader = ScalpPaperTrader(
                initial_balance=config.paper.initial_balance,
                state_file=getattr(
                    getattr(config, "scalp", None),
                    "state_file",
                    "scalp_paper_state.json",
                ),
            )
            self._executor: Optional[OrderExecutor] = None
            logger.info(
                "Paper mode: balance = %s",
                format_usd(config.paper.initial_balance),
            )
        else:
            self._paper_trader = None
            self._executor = OrderExecutor(
                private_key=config.credentials.private_key,
                api_key=config.credentials.api_key,
                api_secret=config.credentials.api_secret,
                api_passphrase=config.credentials.api_passphrase,
                funder_address=config.credentials.funder_address,
                signature_type=config.credentials.signature_type,
            )
            logger.info("Live mode: executor initialised")

        # Track price-to-beat per window per asset
        self._ptb_cache: dict = {}  # (asset, window_start) -> ptb_price
        self._ptb_lock = threading.Lock()

    def _build_strategy(self, config: Config) -> ScalpStrategy:
        """Build ScalpStrategy from config, using defaults if ScalpConfig missing."""
        sc = getattr(config, "scalp", None)
        if sc:
            return ScalpStrategy(
                btc_min_spread=sc.btc_min_spread,
                eth_min_spread=sc.eth_min_spread,
                sol_min_spread=sc.sol_min_spread,
                min_velocity_pct=sc.min_velocity_pct,
                min_secs_remaining=sc.min_secs_remaining,
                poly_prob_low=sc.poly_prob_low,
                poly_prob_high=sc.poly_prob_high,
                take_profit_pct=sc.take_profit_pct,
                stop_loss_pct=sc.stop_loss_pct,
                max_hold_seconds=sc.max_hold_seconds,
                emergency_exit_secs=sc.emergency_exit_secs,
                max_positions_per_asset=sc.max_positions_per_asset,
                max_total_positions=sc.max_total_positions,
                max_daily_loss=sc.max_daily_loss,
                loss_cooldown_secs=sc.loss_cooldown_secs,
                position_size_pct=sc.position_size_pct,
            )
        return ScalpStrategy()  # all defaults

    # ------------------------------------------------------------------
    # Startup / Shutdown
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the bot and run until shutdown."""
        self._running = True

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # Start price feed
        self._price_feed.start()

        # Start CLOB heartbeat (live mode)
        if self._executor:
            self._executor.start_heartbeat()

        # Start watchdog
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="scalp-watchdog",
        )
        self._watchdog_thread.start()

        # Wait for price data
        logger.info("Waiting up to %ds for price feed…", self.STARTUP_WAIT)
        if not self._price_feed.wait_for_data(timeout=self.STARTUP_WAIT):
            logger.warning("Price feed not ready after %ds — continuing", self.STARTUP_WAIT)
        else:
            logger.info("Price feed ready.")

        self._send_startup_message()
        self._run_loop()

    def shutdown(self, reason: str = "User requested shutdown") -> None:
        """Gracefully shut down."""
        if not self._running:
            return

        logger.info("Shutting down: %s", reason)
        self._running = False
        self._shutdown_event.set()

        # Close any open positions at market (emergency)
        self._emergency_close_all()

        self._price_feed.stop()
        if self._executor:
            self._executor.stop_heartbeat()
            try:
                self._executor.cancel_all_orders()
            except Exception:
                pass

        # Final stats
        self._log_final_stats()
        logger.info("Scalp bot stopped.")

    def _handle_signal(self, signum, frame) -> None:
        logger.info("Signal %s received — shutting down…", signum)
        self.shutdown(f"Signal {signum}")

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        """Monitor main loop health."""
        self._shutdown_event.wait(timeout=self.WATCHDOG_TIMEOUT)
        while self._running:
            age = time.time() - self._last_loop_ts
            if age > self.WATCHDOG_TIMEOUT:
                msg = (
                    f"SCALP WATCHDOG: Main loop stalled for {age:.0f}s "
                    f"(threshold={self.WATCHDOG_TIMEOUT}s)"
                )
                logger.error(msg)
                self._send_telegram(msg)
            self._shutdown_event.wait(timeout=self.WATCHDOG_TIMEOUT / 2)

    # ------------------------------------------------------------------
    # Main Loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Main 2-second evaluation loop."""
        logger.info("Starting scalp loop (interval=%.1fs)", self._loop_interval)

        while self._running:
            loop_start = time.time()
            self._cycle_count += 1

            try:
                self._evaluate_cycle()
            except Exception as exc:
                logger.error("Unhandled error in scalp loop: %s", exc, exc_info=True)

            # Periodic health report
            try:
                if time.time() - self._last_health_report > self.HEALTH_REPORT_INTERVAL:
                    self._send_health_report()
                    self._last_health_report = time.time()
            except Exception as exc:
                logger.error("Health report error: %s", exc)

            # Watchdog heartbeat
            self._last_loop_ts = time.time()

            # Precise sleep
            elapsed = time.time() - loop_start
            sleep_time = max(0.1, self._loop_interval - elapsed)
            self._shutdown_event.wait(timeout=sleep_time)

    # ------------------------------------------------------------------
    # Evaluation cycle
    # ------------------------------------------------------------------

    def _evaluate_cycle(self) -> None:
        """
        One full evaluation cycle:
          1. Find active markets
          2. For each market: check exits, then check entries
        """
        # Find active markets
        try:
            markets = self._market_finder.find_active_5min_markets()
        except Exception as exc:
            logger.warning("Could not fetch markets: %s", exc)
            return

        if not markets:
            logger.debug("Cycle %d: no active markets", self._cycle_count)
            return

        secs_left = seconds_until_window_end()
        if self._cycle_count % 30 == 1:  # Log every ~60s
            logger.info(
                "Cycle %d: %d markets, %.0fs left in window",
                self._cycle_count, len(markets), secs_left,
            )

        # Resolve any late-mode positions whose window has ended
        try:
            self._resolve_expired_positions()
        except Exception as exc:
            logger.error("Error resolving expired positions: %s", exc)

        for market in markets:
            try:
                self._evaluate_market(market, secs_left)
            except Exception as exc:
                logger.error(
                    "Error evaluating %s: %s",
                    market.get("slug", "?"), exc, exc_info=True,
                )

    # BTC-only: highest liquidity, biggest dollar moves, proven edge
    SCALP_ASSETS = {"BTC"}

    def _evaluate_market(self, market: dict, secs_remaining: float) -> None:
        """Evaluate a single market: exits first, then entries."""
        asset = market.get("asset", "")

        # Only scalp assets with sufficient liquidity
        if asset not in self.SCALP_ASSETS:
            return

        token_id_yes = market.get("token_id_yes", "")

        if not token_id_yes:
            return

        # Fetch current Polymarket midpoint
        poly_mid_yes = self._get_polymarket_mid(token_id_yes)
        if poly_mid_yes <= 0:
            logger.debug("[%s] Could not get midpoint", asset)
            return

        # --- STEP 1: Check exits ---
        open_positions = self._get_open_positions_for_asset(asset)
        for position in open_positions:
            # CRITICAL: fetch midpoint using the POSITION's token, not the
            # current market's token. When windows change, the market's
            # token_id_yes points to the NEW window, but the position
            # holds tokens from the OLD window.
            pos_mid = self._get_position_mid(position)
            if pos_mid is None:
                continue
            exit_signal = self._strategy.check_exit(
                position=position,
                current_poly_mid_yes=pos_mid,
                secs_remaining=secs_remaining,
            )
            if exit_signal:
                self._execute_exit(position, exit_signal)

        # --- STEP 2: Check entries ---
        # Get exchange price
        exchange_price = self._price_feed.get_current_price(asset)
        if exchange_price <= 0:
            return

        # Get price-to-beat
        ptb = self._get_price_to_beat(asset, market)
        if ptb is None or ptb <= 0:
            return

        # EARLY MODE DISABLED — backtest showed 39% WR, -$668 P&L
        # Only late mode is profitable (51% WR, +$183 in backtest)
        # entry_signal = self._strategy.check_entry(...)

        # --- STEP 2b: Check late-window entry (confirmed direction, hold to expiry) ---
        late_signal = self._strategy.check_late_entry(
            asset=asset,
            exchange_price=exchange_price,
            price_to_beat=ptb,
            poly_mid_yes=poly_mid_yes,
            secs_remaining=secs_remaining,
            market=market,
        )

        if late_signal:
            self._execute_entry(market, late_signal)

    # ------------------------------------------------------------------
    # Entry execution
    # ------------------------------------------------------------------

    def _execute_entry(self, market: dict, signal: ScalpSignal) -> None:
        """Execute a scalp entry (buy tokens)."""
        balance = self._get_balance()
        size = self._strategy.risk_manager.calculate_position_size(balance)

        if size < 1.0:
            logger.info("[%s] Position size too small: %s", signal.asset, format_usd(size))
            return

        if self._config.is_paper_mode and self._paper_trader:
            result = self._paper_trader.simulate_buy(
                market=market,
                side=signal.direction,
                size_usd=size,
                price=signal.entry_price,
                mode=signal.mode,
            )

            if result.success:
                self._strategy.risk_manager.register_position(
                    signal.asset, result.position_id,
                )
                self._send_entry_alert(signal, result.position_id, size, result.shares)
            else:
                logger.info("[%s] Paper buy failed: %s", signal.asset, result.error)

        elif self._executor and self._executor.is_ready:
            # Live mode: place FOK order
            order_result = self._executor.place_order(
                market=market,
                side=signal.direction,
                size=size,
                price=signal.entry_price,
                order_type="FOK",
            )
            if order_result.success:
                # In live mode, we need to track the position manually
                position_id = order_result.order_id or f"LIVE-{int(time.time())}"
                self._strategy.risk_manager.register_position(
                    signal.asset, position_id,
                )
                self._send_entry_alert(signal, position_id, size, order_result.filled_size)
            else:
                logger.warning("[%s] Live buy failed: %s", signal.asset, order_result.error)

    # ------------------------------------------------------------------
    # Exit execution
    # ------------------------------------------------------------------

    def _execute_exit(self, position: ScalpPosition, exit_signal: ExitSignal) -> None:
        """Execute a scalp exit (sell tokens)."""
        if self._config.is_paper_mode and self._paper_trader:
            result = self._paper_trader.simulate_sell(
                position_id=position.position_id,
                exit_reason=exit_signal.reason,
            )

            if result.success:
                self._strategy.risk_manager.close_position(
                    position.asset, position.position_id, result.net_pnl,
                )
                self._send_exit_alert(position, exit_signal, result.net_pnl, result.pnl_pct)
            else:
                logger.warning(
                    "[%s] Paper sell failed: %s", position.asset, result.error,
                )

        elif self._executor and self._executor.is_ready:
            # Live mode: place sell order
            # Build a market dict for the sell
            sell_market = {
                "token_id_yes": position.token_id if position.direction == "YES" else "",
                "token_id_no": position.token_id if position.direction == "NO" else "",
                "slug": f"scalp-exit-{position.position_id}",
                "asset": position.asset,
            }
            sell_price = exit_signal.exit_price
            order_result = self._executor.place_order(
                market=sell_market,
                side=position.direction,
                size=position.shares * sell_price,
                price=sell_price,
                order_type="FOK",
            )
            if order_result.success:
                net_pnl = (sell_price - position.entry_price) * position.shares
                self._strategy.risk_manager.close_position(
                    position.asset, position.position_id, net_pnl,
                )
                pnl_pct = (sell_price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0
                self._send_exit_alert(position, exit_signal, net_pnl, pnl_pct)
            else:
                logger.warning(
                    "[%s] Live sell failed: %s", position.asset, order_result.error,
                )

    # ------------------------------------------------------------------
    # Emergency close
    # ------------------------------------------------------------------

    def _emergency_close_all(self) -> None:
        """Close all open positions on shutdown."""
        if not self._paper_trader:
            return
        positions = self._paper_trader.get_open_positions()
        for position in positions:
            logger.info("Emergency close: %s %s %s", position.position_id, position.asset, position.direction)
            try:
                result = self._paper_trader.simulate_sell(
                    position_id=position.position_id,
                    exit_reason="shutdown",
                )
                if result.success:
                    self._strategy.risk_manager.close_position(
                        position.asset, position.position_id, result.net_pnl,
                    )
            except Exception as exc:
                logger.error("Emergency close failed for %s: %s", position.position_id, exc)

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    def _get_position_mid(self, position) -> Optional[float]:
        """
        Get the YES-equivalent midpoint for a position's token.
        
        For YES positions: returns the token's mid directly.
        For NO positions: fetches the NO token mid and returns (1 - NO_mid)
        so that check_exit can use its standard YES-perspective math.
        
        Actually simpler: we always need the YES mid of the SAME market.
        The position stores the token_id of the side it bought.
        For YES: that IS the YES token, fetch its mid.
        For NO: we need the YES mid of the same market. But we don't
        store the YES token_id on NO positions.
        
        Simplest correct approach: fetch the position's token mid directly
        and pass it as-is. Then in check_exit, treat it as our-side price.
        """
        token_id = position.token_id
        if not token_id:
            return None
        
        try:
            mid = self._get_polymarket_mid(token_id)
            if mid <= 0:
                return None
            
            # mid is the price of the token we hold.
            # check_exit expects YES mid and converts internally.
            # If we hold YES, mid IS the YES mid -> pass as-is.
            # If we hold NO, mid is the NO token price.
            #   check_exit does: current_price = 1.0 - yes_mid
            #   So we need to pass yes_mid = 1.0 - no_mid
            if position.direction == "NO":
                return 1.0 - mid  # convert NO mid to YES-equivalent
            return mid
        except Exception as exc:
            logger.debug("Could not get position mid for %s: %s", token_id[:16], exc)
            return None

    def _get_polymarket_mid(self, token_id: str) -> float:
        """Fetch YES token midpoint from CLOB."""
        if self._executor:
            return self._executor.get_midpoint(token_id)

        try:
            with httpx.Client(timeout=6) as client:
                resp = client.get(
                    f"{CLOB_BASE}/midpoint",
                    params={"token_id": token_id},
                )
                if resp.status_code == 200:
                    return float(resp.json().get("mid", 0.5) or 0.5)
        except Exception as exc:
            logger.debug("Midpoint fetch error: %s", exc)
        return 0.0

    def _get_price_to_beat(self, asset: str, market: dict) -> Optional[float]:
        """
        Get the price-to-beat for the current window.

        The PtB is the exchange price at the start of the 5-min window.
        We cache it per (asset, window_start) so we only look it up once.
        """
        window_start = market.get("window_start", 0)
        cache_key = (asset, window_start)

        with self._ptb_lock:
            if cache_key in self._ptb_cache:
                return self._ptb_cache[cache_key]

        # Try to get it from price feed history
        history = self._price_feed.get_price_history(asset, minutes=6)
        if not history:
            return None

        # Find the price closest to window_start
        best_price = None
        best_delta = float("inf")
        for ts, price in history:
            delta = abs(ts - window_start)
            if delta < best_delta:
                best_delta = delta
                best_price = price

        # Accept if within 30 seconds of window start
        if best_price and best_delta < 30:
            with self._ptb_lock:
                self._ptb_cache[cache_key] = best_price
            return best_price

        # Fallback: use the oldest price in history as approximation
        if history:
            oldest_price = history[0][1]
            with self._ptb_lock:
                self._ptb_cache[cache_key] = oldest_price
            return oldest_price

        return None

    def _resolve_expired_positions(self) -> None:
        """
        Resolve late-mode positions that held to window expiry.

        These positions weren't sold on the book — they need binary
        resolution ($1 if won, $0 if lost) just like the old bot.
        """
        if not self._paper_trader:
            return

        positions = self._paper_trader.get_open_positions()
        now = time.time()

        for position in positions:
            if position.mode != "late":
                continue

            # Check if this position's window has ended
            # We don't store window_end on ScalpPosition, so estimate:
            # The position was entered with some secs_remaining.
            # If we've held for more than 5 minutes, the window has definitely ended.
            hold_time = now - position.entry_time
            if hold_time < 180:  # less than 3 min — window probably still open
                continue

            # Check the exchange price vs price-to-beat to determine outcome
            exchange_price = self._price_feed.get_current_price(position.asset)
            if exchange_price <= 0:
                continue

            # Get the price-to-beat from our cache or estimate
            # Since the window has ended, we need to figure out if price ended up or down
            # For simplicity, check the current state — if we've held 3+ min,
            # the window has resolved. Use the last known exchange price.
            
            # Determine if this position won based on market resolution
            # We'll use the paper_trader's simulate_sell with the resolution price
            # If direction=YES and exchange > ptb, won (exit at ~$1)
            # If direction=NO and exchange < ptb, won (exit at ~$1)
            # Otherwise lost (exit at ~$0)

            # For now, trigger a sell at current market price
            # The token should be near $1 or $0 after resolution
            result = self._paper_trader.simulate_sell(
                position_id=position.position_id,
                exit_reason="expiry_resolution",
            )

            if result.success:
                self._strategy.risk_manager.close_position(
                    position.asset, position.position_id, result.net_pnl,
                )
                # Build exit signal for notification
                pnl_pct = result.pnl_pct
                exit_signal = ExitSignal(
                    reason="expiry_resolution",
                    exit_price=result.sell_price,
                    pnl_pct=pnl_pct,
                )
                self._send_exit_alert(position, exit_signal, result.net_pnl, pnl_pct)
                logger.info(
                    "[%s] Late position resolved: %s pnl=$%.2f",
                    position.asset, position.position_id, result.net_pnl,
                )

    def _get_balance(self) -> float:
        """Get current trading balance."""
        if self._paper_trader:
            return self._paper_trader.get_balance()
        if self._executor:
            return self._executor.get_balance()
        return 500.0

    def _get_open_positions_for_asset(self, asset: str) -> list:
        """Get open positions for an asset."""
        if self._paper_trader:
            return self._paper_trader.get_open_positions_for_asset(asset)
        return []

    # ------------------------------------------------------------------
    # Telegram notifications
    # ------------------------------------------------------------------

    def _send_telegram(self, message: str) -> None:
        """Send a Telegram message if configured."""
        cfg = self._config
        if cfg.telegram and cfg.telegram.is_configured:
            try:
                send_telegram_message(
                    cfg.telegram.bot_token,
                    cfg.telegram.chat_id,
                    message,
                )
            except Exception as exc:
                logger.warning("Telegram send failed: %s", exc)

    def _send_entry_alert(
        self,
        signal: ScalpSignal,
        position_id: str,
        size_usd: float,
        shares: float,
    ) -> None:
        """Send scalp entry notification."""
        direction_label = "Up" if signal.direction == "YES" else "Down"
        balance = self._get_balance()

        # Use mode-specific exit params for target/stop display
        if signal.mode == "late":
            sl = self._strategy.LATE_STOP_LOSS
            mode_label = "LATE"
            target_str = "$1.00 (hold to expiry)"
        else:
            tp = self._strategy._take_profit_pct
            sl = self._strategy._stop_loss_pct
            mode_label = "EARLY"
            target_str = f"{round_to_tick(signal.entry_price * (1 + tp)):.2f} (+{tp*100:.0f}%)"

        stop_price = round_to_tick(signal.entry_price * (1 - sl))

        msg = (
            f"SCALP ENTRY [{mode_label}]\n"
            f"Asset: {signal.asset}\n"
            f"Direction: {signal.direction} ({direction_label})\n"
            f"Entry: {signal.entry_price:.2f}\n"
            f"Size: ${size_usd:.2f}\n"
            f"Shares: {shares:.2f}\n"
            f"Spread: {signal.spread:+.2f}\n"
            f"Target: {target_str}\n"
            f"Stop: {stop_price:.2f} (-{sl*100:.0f}%)\n"
            f"Balance: ${balance:.2f}"
        )
        logger.info(msg.replace("\n", " | "))
        self._send_telegram(msg)

    def _send_exit_alert(
        self,
        position: ScalpPosition,
        exit_signal: ExitSignal,
        net_pnl: float,
        pnl_pct: float,
    ) -> None:
        """Send scalp exit notification."""
        hold_time = time.time() - position.entry_time
        balance = self._get_balance()

        if net_pnl >= 0:
            header = "SCALP EXIT — PROFIT"
        else:
            header = "SCALP EXIT — LOSS"

        reason_labels = {
            "take_profit": "Take Profit",
            "stop_loss": "Stop Loss",
            "max_hold": "Max Hold Time",
            "window_ending": "Window Ending",
            "expiry_resolution": "Held to Expiry",
            "shutdown": "Bot Shutdown",
        }
        reason_label = reason_labels.get(exit_signal.reason, exit_signal.reason)

        msg = (
            f"{header}\n"
            f"Asset: {position.asset}\n"
            f"Direction: {position.direction}\n"
            f"Entry: {position.entry_price:.2f} -> Exit: {exit_signal.exit_price:.2f}\n"
            f"P&L: ${net_pnl:+.2f} ({pnl_pct*100:+.1f}%)\n"
            f"Hold time: {hold_time:.0f}s\n"
            f"Reason: {reason_label}\n"
            f"Balance: ${balance:.2f}"
        )
        logger.info(msg.replace("\n", " | "))
        self._send_telegram(msg)

    def _send_startup_message(self) -> None:
        """Send startup notification."""
        balance = self._get_balance()
        daily_pnl = self._strategy.risk_manager.get_daily_pnl()
        open_positions = self._strategy.risk_manager.get_total_open()

        msg = (
            f"Polymarket Scalp Bot Started\n"
            f"Mode: {self._config.trading_mode.upper()}\n"
            f"Assets: {', '.join(self._config.strategy.assets)}\n"
            f"Loop: {self._loop_interval}s\n"
            f"Balance: {format_usd(balance)}\n"
            f"Open Positions: {open_positions}\n"
            f"Daily P&L: {format_usd(daily_pnl)}"
        )
        self._send_telegram(msg)
        logger.info(msg.replace("\n", " | "))

    def _send_health_report(self) -> None:
        """Send periodic health report."""
        uptime = time.time() - self._start_time
        uptime_str = f"{uptime/3600:.1f}h"
        balance = self._get_balance()
        daily_pnl = self._strategy.risk_manager.get_daily_pnl()
        open_count = self._strategy.risk_manager.get_total_open()
        halted = self._strategy.risk_manager.is_halted()

        stats_str = ""
        if self._paper_trader:
            stats = self._paper_trader.get_stats()
            total = stats.get("total_trades", 0)
            wins = stats.get("wins", 0)
            wr = stats.get("win_rate", 0)
            stats_str = f"\nTrades: {total} ({wins}W, WR {wr*100:.0f}%)"

        msg = (
            f"SCALP HEALTH\n"
            f"Uptime: {uptime_str}\n"
            f"Cycles: {self._cycle_count}\n"
            f"Balance: {format_usd(balance)}\n"
            f"Daily P&L: {format_usd(daily_pnl)}\n"
            f"Open: {open_count}\n"
            f"Halted: {'YES' if halted else 'No'}"
            f"{stats_str}"
        )
        logger.info("Health: %s", msg.replace("\n", " | "))
        self._send_telegram(msg)

    def _log_final_stats(self) -> None:
        """Log final stats on shutdown."""
        if not self._paper_trader:
            return
        stats = self._paper_trader.get_stats()
        logger.info("Final stats: %s", stats)

        msg_parts = ["SCALP BOT SHUTDOWN"]
        msg_parts.append(f"Balance: {format_usd(stats.get('balance', 0))}")
        msg_parts.append(f"Total trades: {stats.get('total_trades', 0)}")
        msg_parts.append(f"Wins: {stats.get('wins', 0)}")
        msg_parts.append(f"Losses: {stats.get('losses', 0)}")
        total_pnl = stats.get("total_pnl", 0)
        msg_parts.append(f"Total P&L: {format_usd(total_pnl)}")
        self._send_telegram("\n".join(msg_parts))


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    """Main entry point for scalp bot."""
    config = load_config()
    bot = ScalpBot(config)

    try:
        bot.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down.")
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        bot.shutdown()


if __name__ == "__main__":
    main()
