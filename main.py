"""
main.py - Main orchestrator for the Polymarket 5-minute crypto trading bot.

Execution loop (runs every ~30 seconds):
  1. Find active 5-minute markets for BTC, ETH, SOL
  2. For each market:
     a. Fetch real-time price from Binance WebSocket
     b. Fetch Polymarket orderbook / midpoint (WebSocket first, REST fallback)
     c. Run strategy evaluation (including late-window maker strategy)
     d. If signal found AND risk manager approves:
        - Calculate position size
        - Execute order with feeRateBps (paper or live)
        - Register open position
  3. Resolve any markets that have ended
  4. Run position merger (capital recovery)
  5. Log hourly / daily reports
  6. Repeat

Upgrades integrated (v2):
  - FeeManager: dynamic feeRateBps included in all order signatures
  - WebSocketFeed: real-time orderbook from Polymarket CLOB WS
  - PositionMerger: automatic YES+NO capital recovery
  - Late-window maker strategy: high-alpha final-seconds trades

Run with:
    python main.py               # uses TRADING_MODE from .env
    TRADING_MODE=paper python main.py
    TRADING_MODE=live  python main.py

Stop with Ctrl+C (SIGINT) or SIGTERM.
"""

import os
import sys
import time
import signal
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

# ── Module imports ──────────────────────────────────────────────────────────
from config import load_config, Config
from data_feeds import PriceFeed
from market_finder import MarketFinder
from strategy import StrategyEngine, TradingSignal
from executor import OrderExecutor, OrderResult
from risk_manager import RiskManager, TradeRecord
from monitor import PerformanceMonitor, TradeLogEntry
from paper_trader import PaperTrader, SimulatedResult
from utils import format_usd, format_pct, seconds_until_window_end, send_telegram_message

# ── Upgrade imports (graceful degradation if unavailable) ────────────────────
try:
    from fee_manager import FeeManager
    _FEE_MANAGER_OK = True
except ImportError:
    FeeManager = None
    _FEE_MANAGER_OK = False
    logger_init = logging.getLogger("main")
    logger_init.warning("fee_manager not available — feeRateBps will default to 0")

try:
    from ws_feed import WebSocketFeed
    _WS_FEED_OK = True
except ImportError:
    WebSocketFeed = None
    _WS_FEED_OK = False

try:
    from position_merger import PositionMerger
    _POSITION_MERGER_OK = True
except ImportError:
    PositionMerger = None
    _POSITION_MERGER_OK = False

try:
    from redeemer import AutoRedeemer
    _REDEEMER_OK = True
except ImportError:
    AutoRedeemer = None
    _REDEEMER_OK = False

logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class PolymarketBot:
    """
    Orchestrates all modules into a running trading bot.
    """

    LOOP_INTERVAL = 30       # seconds between full evaluation cycles
    STARTUP_WAIT = 20        # seconds to wait for price feed to warm up

    def __init__(self, config: Config):
        self._config = config
        self._running = False
        self._shutdown_event = threading.Event()

        logger.info("=" * 60)
        logger.info("Polymarket 5-Min Crypto Bot v2 — %s mode", config.trading_mode.upper())
        logger.info("=" * 60)

        # ── Upgrade 1: FeeManager ─────────────────────────────────────────
        if _FEE_MANAGER_OK and config.fee.dynamic_fee_enabled:
            self._fee_manager = FeeManager()
            logger.info("FeeManager: enabled (dynamic feeRateBps querying active)")
        else:
            self._fee_manager = None
            logger.info("FeeManager: disabled (feeRateBps will default to 0)")

        # ── Upgrade 2: WebSocket Orderbook Feed ───────────────────────────
        if _WS_FEED_OK and config.polymarket_ws.enabled:
            self._ws_feed = WebSocketFeed()
            logger.info("WebSocketFeed: enabled (real-time CLOB orderbook)")
        else:
            self._ws_feed = None
            logger.info("WebSocketFeed: disabled (using REST polling)")

        # ── Upgrade 3: PositionMerger ─────────────────────────────────────
        if _POSITION_MERGER_OK and config.merger.enabled:
            self._position_merger = PositionMerger(
                paper_mode=config.is_paper_mode,
            )
            logger.info("PositionMerger: enabled")
        else:
            self._position_merger = None
            logger.info("PositionMerger: disabled")

        # ── Upgrade 4: AutoRedeemer ───────────────────────────────────────
        if _REDEEMER_OK:
            self._redeemer = AutoRedeemer(
                paper_mode=config.is_paper_mode,
            )
            logger.info("AutoRedeemer: enabled — winnings will be collected automatically")
        else:
            self._redeemer = None
            logger.info("AutoRedeemer: disabled")

        # Initialise core modules
        self._price_feed = PriceFeed(
            assets=config.strategy.assets,
            history_minutes=config.exchange.price_history_minutes,
        )

        self._market_finder = MarketFinder(
            assets=config.strategy.assets,
            gamma_base=config.api.gamma_base,
        )

        # ── Upgrade 4: Pass FeeManager to StrategyEngine ──────────────────
        self._strategy = StrategyEngine(
            price_feed=self._price_feed,
            strategy_config=config.strategy,
            risk_config=config.risk,
            fee_manager=self._fee_manager,
        )

        self._monitor = PerformanceMonitor(
            config=config.monitor,
            telegram_config=config.telegram,
        )

        # Paper or live mode
        if config.is_paper_mode:
            self._paper_trader = PaperTrader(
                initial_balance=config.paper.initial_balance,
                state_file=config.paper.state_file,
            )
            self._executor: Optional[OrderExecutor] = None
            self._risk_manager = RiskManager(
                initial_balance=config.paper.initial_balance,
                config=config.risk,
            )
            logger.info(
                "Paper mode: virtual balance = %s",
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
            # Wire FeeManager into executor so orders include feeRateBps
            if self._fee_manager is not None:
                self._executor._fee_manager = self._fee_manager
                logger.info("FeeManager wired into OrderExecutor for live order signing")

            # Fetch real balance from exchange
            live_balance = self._executor.get_balance()
            if live_balance <= 0:
                logger.warning(
                    "Could not fetch live balance — using default $500. "
                    "Check credentials."
                )
                live_balance = 500.0
            self._risk_manager = RiskManager(
                initial_balance=live_balance,
                config=config.risk,
            )
            logger.info("Live mode: on-chain balance = %s", format_usd(live_balance))

            # Wire PositionMerger with executor for live scans
            if self._position_merger is not None:
                self._position_merger._executor = self._executor

        # Stats
        self._cycle_count = 0
        self._start_time = time.time()

    # ------------------------------------------------------------------
    # Startup / Shutdown
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the bot and run until shutdown."""
        self._running = True

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # Start price feed WebSocket (Binance)
        self._price_feed.start()

        # Start CLOB heartbeat (live mode only)
        if self._executor:
            self._executor.start_heartbeat()

        # ── Upgrade 2: Start Polymarket WebSocket feed ────────────────────
        if self._ws_feed is not None:
            self._ws_feed.start()
            # Subscribe to tokens from the first set of markets we can find
            # Additional subscriptions happen in _evaluate_cycle as markets are discovered
            logger.info("WebSocketFeed started — will subscribe to market tokens on first cycle")

        # Wait for price data
        logger.info("Waiting up to %ds for Binance price feed…", self.STARTUP_WAIT)
        if not self._price_feed.wait_for_data(timeout=self.STARTUP_WAIT):
            logger.warning(
                "Price feed not ready after %ds — continuing without full warmup",
                self.STARTUP_WAIT,
            )
        else:
            logger.info("Price feed ready.")

        self._send_startup_message()
        self._run_loop()

    def shutdown(self, reason: str = "User requested shutdown") -> None:
        """Gracefully shut down the bot."""
        if not self._running:
            return

        logger.info("Shutting down: %s", reason)
        self._running = False
        self._shutdown_event.set()

        # Stop services
        self._price_feed.stop()
        if self._executor:
            self._executor.stop_heartbeat()
            try:
                self._executor.cancel_all_orders()
            except Exception:
                pass

        # Stop WebSocket feed
        if self._ws_feed is not None:
            self._ws_feed.stop()

        # Close FeeManager HTTP client
        if self._fee_manager is not None:
            self._fee_manager.close()

        # Final position merger summary
        if self._position_merger is not None:
            total_merged = self._position_merger.total_merged_usd
            if total_merged > 0:
                logger.info("PositionMerger: total USDC recovered = %s", format_usd(total_merged))

        # Final report
        logger.info("\n%s", self._monitor.get_summary())
        logger.info("Bot stopped.")

    def _handle_signal(self, signum, frame) -> None:
        logger.info("Signal %s received — shutting down…", signum)
        self.shutdown(f"Signal {signum}")

    # ------------------------------------------------------------------
    # Main Loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Main evaluation loop — runs every LOOP_INTERVAL seconds."""
        logger.info("Starting main loop (interval=%ds)", self.LOOP_INTERVAL)

        while self._running:
            loop_start = time.time()
            self._cycle_count += 1

            try:
                self._evaluate_cycle()
            except Exception as exc:
                logger.error("Unhandled exception in main loop: %s", exc, exc_info=True)

            # Print periodic reports
            if self._monitor.should_print_hourly_report():
                report = self._monitor.get_hourly_report()
                logger.info("\n%s", report)

            if self._monitor.should_print_daily_report():
                report = self._monitor.get_daily_report()
                logger.info("\n%s", report)

            # Wait for next cycle (precise timing)
            elapsed = time.time() - loop_start
            sleep_time = max(0.1, self.LOOP_INTERVAL - elapsed)
            self._shutdown_event.wait(timeout=sleep_time)

    # ------------------------------------------------------------------
    # Evaluation Cycle
    # ------------------------------------------------------------------

    def _evaluate_cycle(self) -> None:
        """
        One full evaluation cycle:
          1. Find active markets
          2. Subscribe new tokens to WebSocket feed
          3. Evaluate each market
          4. Execute trades if applicable
          5. Resolve paper positions
          6. Run position merger (capital recovery)
        """
        # Step 1: Resolve any finished paper positions first
        if self._paper_trader:
            self._resolve_paper_positions()

        # Step 2: Find active markets
        try:
            markets = self._market_finder.find_active_5min_markets()
        except Exception as exc:
            logger.warning("Could not fetch active markets: %s", exc)
            markets = []

        if not markets:
            logger.debug("Cycle %d: no active markets found", self._cycle_count)
            return

        secs_left = seconds_until_window_end()
        logger.debug(
            "Cycle %d: %d markets found, %.0fs left in window",
            self._cycle_count, len(markets), secs_left,
        )

        # Step 3: Subscribe new market tokens to the WebSocket feed
        if self._ws_feed is not None:
            self._subscribe_markets_to_ws(markets)

        # Step 4: Evaluate each market
        for market in markets:
            try:
                self._evaluate_market(market)
            except Exception as exc:
                logger.error(
                    "Error evaluating market %s: %s",
                    market.get("slug", "?"), exc,
                    exc_info=True,
                )

        # Step 5: Run position merger if enabled
        if self._position_merger is not None:
            cfg = self._config.merger
            if self._cycle_count % cfg.merge_check_interval_cycles == 0:
                try:
                    self._run_position_merger()
                except Exception as exc:
                    logger.warning("Position merger error: %s", exc)

        # Step 6: Auto-redeem winning tokens from resolved markets
        if self._redeemer is not None:
            try:
                self._run_auto_redeem()
            except Exception as exc:
                logger.warning("Auto-redeem error: %s", exc)

    def _run_auto_redeem(self) -> None:
        """Check resolved markets and redeem winning tokens → USDC."""
        if self._redeemer is None:
            return
        # Collect recently traded market slugs from the monitor
        recent_slugs = []
        for trade in self._monitor._trades[-50:]:
            if trade.market_slug and trade.market_slug not in recent_slugs:
                recent_slugs.append(trade.market_slug)
        if not recent_slugs:
            return
        # Build positions dict from paper/live state
        positions = {}  # TODO: populate from executor or paper_trader state
        results = self._redeemer.check_and_redeem(recent_slugs, positions)
        for r in results:
            if r.success:
                logger.info(
                    "\U0001f4e5 Redeemed %s %s: %.2f tokens → $%.2f USDC",
                    r.asset, r.outcome, r.tokens_redeemed, r.usdc_received,
                )
                if self._config.telegram and self._config.telegram.is_configured:
                    send_telegram_message(
                        self._config.telegram.bot_token,
                        self._config.telegram.chat_id,
                        f"\U0001f4e5 Redeemed {r.asset} {r.outcome}: "
                        f"${r.usdc_received:,.2f} USDC collected",
                    )

    def _subscribe_markets_to_ws(self, markets: list) -> None:
        """Subscribe active market tokens to the WebSocket feed."""
        if self._ws_feed is None:
            return
        tokens_to_subscribe = []
        for market in markets:
            for key in ("token_id_yes", "token_id_no"):
                tid = market.get(key, "")
                if tid and self._ws_feed.get_orderbook(tid) is None:
                    tokens_to_subscribe.append(tid)
        if tokens_to_subscribe:
            self._ws_feed.subscribe(tokens_to_subscribe)
            logger.debug("WebSocket: subscribed to %d new tokens", len(tokens_to_subscribe))

    def _run_position_merger(self) -> None:
        """Run position merger to recover capital from YES+NO pairs."""
        if self._position_merger is None:
            return

        if self._config.is_paper_mode:
            # In paper mode, we don't have real YES+NO pairs to merge
            # but we can log the intent for testing
            logger.debug("PositionMerger: paper mode — skipping live merge scan")
        else:
            # Live mode: scan and merge real positions
            recovered = self._position_merger.scan_and_merge(self._executor)
            if recovered > 0:
                # Update risk manager balance
                current_balance = self._executor.get_balance()
                if current_balance > 0:
                    self._risk_manager.update_balance(current_balance)

    def _evaluate_market(self, market: dict) -> None:
        """Evaluate a single market and trade if appropriate."""
        asset = market.get("asset", "")
        slug = market.get("slug", "")

        # Get Polymarket midpoint for YES (Up) token
        token_id_yes = market.get("token_id_yes", "")
        if not token_id_yes:
            logger.debug("No YES token_id for %s — skipping", slug)
            return

        # ── Upgrade 2: Try WebSocket feed first, fall back to REST ─────────
        polymarket_mid = self._get_polymarket_mid(token_id_yes)

        if polymarket_mid <= 0:
            logger.debug("Could not fetch midpoint for %s", slug)
            return

        # Check price feed health
        if not self._price_feed.has_data(asset):
            logger.debug("No price data for %s — skipping", asset)
            return

        data_age = self._price_feed.data_age_seconds(asset)
        if data_age > 10:
            logger.warning("%s price data is %.1fs old — may be stale", asset, data_age)

        # Run strategy
        signal: TradingSignal = self._strategy.evaluate(market, polymarket_mid)

        if not signal.is_valid:
            logger.info("[%s] No signal: %s", asset, signal.reasoning)
            return

        logger.info("[%s] Signal: %s", asset, signal)

        # Risk check
        can_trade, reason = self._risk_manager.can_trade()
        if not can_trade:
            logger.info("[%s] Trade blocked: %s", asset, reason)
            return

        if not self._strategy.should_trade(signal):
            logger.debug("[%s] Signal below threshold", asset)
            return

        # Calculate position size
        balance = (
            self._paper_trader.get_balance()
            if self._paper_trader
            else self._risk_manager.get_balance()
        )

        size = self._risk_manager.calculate_position_size(
            edge=signal.edge,
            confidence=signal.confidence,
            balance=balance,
        )

        if size < self._config.risk.min_position_usd:
            logger.info("[%s] Position size too small: %s", asset, format_usd(size))
            return

        # Determine entry price
        token_to_buy = (
            market.get("token_id_yes") if signal.direction == "YES"
            else market.get("token_id_no")
        )
        entry_price = signal.suggested_price
        if not entry_price or entry_price <= 0:
            entry_price = polymarket_mid if signal.direction == "YES" else (1 - polymarket_mid)

        # Execute
        if self._config.is_paper_mode:
            self._execute_paper(market, signal, size, entry_price)
        else:
            self._execute_live(market, signal, size, entry_price)

    # ------------------------------------------------------------------
    # Paper Execution
    # ------------------------------------------------------------------

    def _execute_paper(
        self,
        market: dict,
        signal: TradingSignal,
        size: float,
        price: float,
    ) -> None:
        """Simulate placing an order in paper mode."""
        result: SimulatedResult = self._paper_trader.simulate_order(
            market=market,
            side=signal.direction,
            size_usd=size,
            price=price,
            strategy=signal.strategy_name,
            edge=signal.edge,
            confidence=signal.confidence,
        )

        if result.success:
            self._risk_manager.open_position(market.get("slug", ""), size)
            logger.info(
                "[PAPER] Order placed: %s %s at %.3f | shares=%.2f | id=%s",
                signal.asset, signal.direction, result.filled_price,
                result.filled_size, result.order_id,
            )
            # Telegram trade alert
            if self._config.telegram and self._config.telegram.is_configured:
                bal = self._paper_trader.get_balance() if self._paper_trader else 0
                msg = (
                    f"NEW TRADE\n"
                    f"Asset: {signal.asset}\n"
                    f"Direction: {signal.direction}\n"
                    f"Entry: {result.filled_price:.3f}\n"
                    f"Size: ${size:.2f}\n"
                    f"Shares: {result.filled_size:.2f}\n"
                    f"Edge: {signal.edge:.3f}\n"
                    f"Strategy: {signal.strategy_name}\n"
                    f"Balance: ${bal:.2f}"
                )
                send_telegram_message(self._config.telegram.bot_token, self._config.telegram.chat_id, msg)
        else:
            logger.info("[PAPER] Order failed: %s", result.error)

    # ------------------------------------------------------------------
    # Live Execution
    # ------------------------------------------------------------------

    def _execute_live(
        self,
        market: dict,
        signal: TradingSignal,
        size: float,
        price: float,
    ) -> None:
        """Place a real order on Polymarket."""
        if not self._executor or not self._executor.is_ready:
            logger.error("Executor not ready for live trading.")
            return

        result: OrderResult = self._executor.place_order(
            market=market,
            side=signal.direction,
            size=size,
            price=price,
            order_type="FOK",
        )

        if result.success:
            self._risk_manager.open_position(market.get("slug", ""), size)
            logger.info(
                "[LIVE] Order placed: %s %s at %.3f | id=%s",
                signal.asset, signal.direction, result.filled_price,
                result.order_id,
            )
            # Telegram trade alert
            if self._config.telegram and self._config.telegram.is_configured:
                bal = self._risk_manager.get_balance()
                msg = (
                    f"LIVE TRADE\n"
                    f"Asset: {signal.asset}\n"
                    f"Direction: {signal.direction}\n"
                    f"Entry: {result.filled_price:.3f}\n"
                    f"Size: ${size:.2f}\n"
                    f"Edge: {signal.edge:.3f}\n"
                    f"Strategy: {signal.strategy_name}\n"
                    f"Balance: ${bal:.2f}"
                )
                send_telegram_message(self._config.telegram.bot_token, self._config.telegram.chat_id, msg)
        else:
            logger.warning("[LIVE] Order failed: %s", result.error)

    # ------------------------------------------------------------------
    # Paper Position Resolution
    # ------------------------------------------------------------------

    def _resolve_paper_positions(self) -> None:
        """Resolve any completed paper positions and record P&L."""
        if not self._paper_trader:
            return

        try:
            resolved_trades = self._paper_trader.resolve_positions()
        except Exception as exc:
            logger.warning("Error resolving paper positions: %s", exc)
            return

        for trade in resolved_trades:
            # Update risk manager balance
            self._risk_manager.update_balance(trade.balance_after)
            self._risk_manager.close_position(trade.market_slug)

            # Build TradeRecord for risk manager
            tr = TradeRecord(
                trade_id=trade.order_id,
                timestamp=trade.resolved_at,
                asset=trade.asset,
                direction=trade.direction,
                strategy=trade.strategy,
                size_usd=trade.size_usd,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                pnl=trade.gross_pnl,
                fees_paid=trade.fees_paid,
                market_slug=trade.market_slug,
                won=trade.won,
            )
            self._risk_manager.record_trade(tr)

            # Build TradeLogEntry for monitor
            ts = datetime.fromtimestamp(trade.resolved_at, tz=timezone.utc).isoformat()
            entry = TradeLogEntry(
                timestamp=ts,
                unix_ts=trade.resolved_at,
                market_slug=trade.market_slug,
                asset=trade.asset,
                strategy=trade.strategy,
                direction=trade.direction,
                size_usd=trade.size_usd,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                shares=trade.shares,
                gross_pnl=trade.gross_pnl,
                fees_paid=trade.fees_paid,
                net_pnl=trade.net_pnl,
                balance_after=trade.balance_after,
                edge=trade.edge,
                confidence=trade.confidence,
                won=trade.won,
            )
            self._monitor.log_trade(entry)

            # Telegram resolution alert
            if self._config.telegram and self._config.telegram.is_configured:
                result_emoji = "WIN" if trade.won else "LOSS"
                msg = (
                    f"TRADE RESOLVED: {result_emoji}\n"
                    f"Asset: {trade.asset}\n"
                    f"Direction: {trade.direction}\n"
                    f"Entry: {trade.entry_price:.3f} -> Exit: {trade.exit_price:.3f}\n"
                    f"P&L: ${trade.net_pnl:+.2f}\n"
                    f"Balance: ${trade.balance_after:.2f}"
                )
                send_telegram_message(self._config.telegram.bot_token, self._config.telegram.chat_id, msg)

    # ------------------------------------------------------------------
    # Midpoint fetching (WebSocket first, then REST fallback)
    # ------------------------------------------------------------------

    def _get_polymarket_mid(self, token_id: str) -> float:
        """
        Fetch the current YES token midpoint price.

        Priority:
          1. WebSocket feed (if connected and data is fresh)
          2. OrderExecutor REST (live mode)
          3. Direct HTTP REST call (paper mode)

        Falls back gracefully if WebSocket is unavailable or stale.
        """
        # ── Upgrade 2: WebSocket fast path ────────────────────────────────
        if self._ws_feed is not None and self._ws_feed.is_connected:
            ws_mid = self._ws_feed.get_mid_price(token_id)
            if ws_mid > 0:
                logger.debug("Mid price for %s: %.3f (via WebSocket)", token_id[:16], ws_mid)
                return ws_mid
            # Fall through to REST if WS data is stale/unavailable

        # REST fallback
        if self._executor:
            return self._executor.get_midpoint(token_id)

        # Paper mode: direct HTTP call to CLOB (no auth required)
        try:
            import httpx
            with httpx.Client(timeout=6) as client:
                resp = client.get(
                    "https://clob.polymarket.com/midpoint",
                    params={"token_id": token_id},
                )
                resp.raise_for_status()
                return float(resp.json().get("mid", 0.5) or 0.5)
        except Exception as exc:
            logger.debug("Could not fetch midpoint for %s: %s", token_id[:16], exc)
            return 0.5

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _send_startup_message(self) -> None:
        """Send a startup notification via Telegram if configured."""
        cfg = self._config
        upgrades = []
        if self._fee_manager is not None:
            upgrades.append("FeeManager")
        if self._ws_feed is not None:
            upgrades.append("WSFeed")
        if self._position_merger is not None:
            upgrades.append("Merger")
        if cfg.late_window.enabled:
            upgrades.append("LateWindow")
        upgrades_str = ", ".join(upgrades) if upgrades else "none"

        msg = (
            f"*Polymarket Bot v2 Started*\n"
            f"Mode: {cfg.trading_mode.upper()}\n"
            f"Assets: {', '.join(cfg.strategy.assets)}\n"
            f"Strategy: {cfg.strategy.primary_strategy}\n"
            f"Upgrades: {upgrades_str}\n"
            f"Balance: {format_usd(self._risk_manager.get_balance())}"
        )
        if cfg.telegram.is_configured:
            send_telegram_message(cfg.telegram.bot_token, cfg.telegram.chat_id, msg)
        logger.info(msg.replace("*", ""))


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    """Main entry point."""
    config = load_config()
    bot = PolymarketBot(config)

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
