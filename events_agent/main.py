"""Entry point — async scheduler orchestration for Events agent."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone

from events_agent.config import EventsConfig
from events_agent.scanner import EventsScanner
from events_agent.analyzer import EventsAnalyzer
from events_agent.executor import EventsExecutor
from events_agent.portfolio import PortfolioManager
from nba_agent.utils import utcnow

logger = logging.getLogger("events_agent")

# Category priority weights — multiply composite scores when ranking markets
CATEGORY_PRIORITY: dict[str, float] = {
    "geopolitics": 1.0,
    "crypto": 1.0,
    "government_policy": 1.0,
    "politics": 0.85,
    "commodities": 0.80,
    "tech_industry": 0.80,
    "economics": 0.65,
    "macro_economics": 0.65,
    "entertainment": 0.60,
    "world_elections": 0.60,
    "other": 0.50,
    "science": 0.50,
    "climate": 0.50,
    "forex": 0.50,
    "futures": 0.50,
}


class EventsAgent:
    """Main events agent orchestrator — runs scan cycles, exit checks, and summaries."""

    def __init__(self) -> None:
        self.config = EventsConfig()
        self.config.ensure_data_dir()

        self.scanner = EventsScanner(self.config)
        self.analyzer = EventsAnalyzer(self.config)
        self.executor = EventsExecutor(self.config)
        self.portfolio = PortfolioManager(self.config)

        # Intelligence manager — drives all entry decisions
        self._intel_manager = None
        try:
            from intelligence.manager import IntelligenceManager
            self._intel_manager = IntelligenceManager(self.config)
            logger.info("Intelligence manager initialized")
        except Exception as e:
            logger.error("Failed to initialize intelligence manager: %s", e)

        # Bankroll state — loaded from shared bankroll file
        self._bankroll_path = self.config.DATA_DIR / "bankroll.json"

        self._shutdown = False

    @property
    def current_bankroll(self) -> float:
        """Read current bankroll from shared state."""
        from nba_agent.utils import load_json
        state = load_json(self._bankroll_path, {})
        return float(state.get("current_bankroll", self.config.STARTING_BANKROLL))

    async def run(self) -> None:
        """Main loop — schedule all recurring tasks."""
        logger.info(
            "Events Agent starting — mode=%s scan_interval=%d min",
            self.config.TRADING_MODE,
            self.config.SCAN_INTERVAL,
        )

        while not self._shutdown:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Main loop error: %s", e, exc_info=True)

            # Wait for next scan interval
            for _ in range(self.config.SCAN_INTERVAL * 60):
                if self._shutdown:
                    break
                await asyncio.sleep(1)

    async def _tick(self) -> None:
        """Single cycle: scan, evaluate, trade, check exits."""
        now = utcnow()

        # --- One-time cleanup: purge positions from broken extreme_pricing strategy ---
        cleanup_flag = self.config.DATA_DIR / ".extreme_pricing_cleanup_done"
        if not cleanup_flag.exists():
            try:
                positions = self.portfolio.load_positions()
                purge_count = 0
                for p in positions:
                    edge_src = getattr(p, "edge_source", "") or ""
                    if p.status == "open" and edge_src == "extreme_pricing":
                        p.status = "closed"
                        p.exit_reason = "emergency_purge_bad_strategy"
                        p.exit_time = now.isoformat()
                        purge_count += 1
                if purge_count > 0:
                    self.portfolio._write_positions(positions)
                    logger.warning("EMERGENCY CLEANUP: purged %d extreme_pricing positions", purge_count)
                else:
                    logger.info("Extreme pricing cleanup: no positions to purge")
                cleanup_flag.write_text("done")
            except Exception as e:
                logger.error("Extreme pricing cleanup failed: %s", e)

        # --- One-time SELL cleanup: sell all live extreme_pricing positions on Polymarket ---
        sell_flag = self.config.DATA_DIR / ".extreme_pricing_sell_done"
        if not sell_flag.exists() and self.config.is_live:
            try:
                import httpx as _httpx
                CLOB_API = "https://clob.polymarket.com"
                positions = self.portfolio.load_positions()
                sold_count = 0
                sold_value = 0.0
                for p in positions:
                    edge_src = getattr(p, "edge_source", "") or ""
                    mode = getattr(p, "mode", "")
                    status = getattr(p, "status", "")
                    # Only sell purged live positions
                    if (status == "closed"
                            and edge_src == "extreme_pricing"
                            and mode == "live"
                            and getattr(p, "exit_reason", "") == "emergency_purge_bad_strategy"):
                        token_id = getattr(p, "token_id", "")
                        shares = getattr(p, "shares", 0)
                        if not token_id or shares <= 0:
                            continue
                        # Get current price
                        try:
                            with _httpx.Client(timeout=5) as hc:
                                resp = hc.get(f"{CLOB_API}/midpoint", params={"token_id": token_id})
                                mid = float(resp.json().get("mid", 0)) if resp.status_code == 200 else 0
                        except Exception:
                            mid = 0
                        if mid <= 0.01:  # Skip if no price or nearly worthless
                            continue
                        # Sell via executor
                        try:
                            order_id = self.executor._execute_live_sell(
                                token_id, shares, getattr(p, "market_id", "")
                            )
                            if order_id:
                                sold_count += 1
                                sold_value += shares * mid
                                p.exit_reason = "sold_extreme_pricing_cleanup"
                                p.exit_price = mid
                                p.pnl = round(shares * mid - (getattr(p, "cost", 0) or 0), 2)
                                logger.info(
                                    "SOLD purged position: %s @ %.3f | ~$%.2f | %s",
                                    getattr(p, "side", ""), mid, shares * mid,
                                    getattr(p, "market_question", "")[:50],
                                )
                                import time as _time
                                _time.sleep(0.5)  # Rate limit
                        except Exception as sell_err:
                            logger.warning("Failed to sell %s: %s",
                                           getattr(p, "market_question", "")[:40], sell_err)
                self.portfolio._write_positions(positions)
                logger.warning(
                    "SELL CLEANUP COMPLETE: sold %d positions, recovered ~$%.2f",
                    sold_count, sold_value,
                )
                sell_flag.write_text(f"sold={sold_count} value={sold_value:.2f}")
            except Exception as e:
                logger.error("Sell cleanup failed: %s", e, exc_info=True)

        # Write last scan time for system health dashboard
        import json as _json
        status_path = self.config.DATA_DIR / "system_status.json"
        try:
            status = _json.loads(status_path.read_text()) if status_path.exists() else {}
        except Exception:
            status = {}
        status["events_last_scan"] = now.isoformat()
        try:
            status_path.write_text(_json.dumps(status, default=str))
        except Exception:
            pass

        # 1. Check for early exits on existing positions
        await self._check_exits()

        # 2. Check for resolved positions
        await self._check_resolutions()

        # 3. Scan for new opportunities
        await self._scan_and_trade()

    async def _scan_and_trade(self) -> None:
        """Scan markets, evaluate edges via intelligence pipeline, and execute trades."""
        # Check bankroll pause state
        from nba_agent.utils import load_json
        bankroll_state = load_json(self._bankroll_path, {})
        if bankroll_state.get("is_paused", False):
            logger.info("Trading paused (stop-loss) — skipping scan")
            return

        # --- MAX POSITIONS GATE ---
        open_positions = self.portfolio.get_open_positions()
        if len(open_positions) >= self.config.MAX_CONCURRENT_POSITIONS:
            logger.info("Max positions reached (%d/%d) — skipping scan",
                        len(open_positions), self.config.MAX_CONCURRENT_POSITIONS)
            return

        # Scan for events markets
        markets = await self.scanner.scan()

        # Sort markets by category priority (highest first) for better capital allocation
        markets.sort(
            key=lambda m: CATEGORY_PRIORITY.get(m.category.value.lower(), 0.50),
            reverse=True,
        )
        logger.info("Evaluating %d events markets (sorted by category priority)", len(markets))

        # --- RUN INTELLIGENCE PIPELINE ---
        intel_report = None
        if self._intel_manager:
            try:
                intel_report = await self._intel_manager.run_scan_cycle(
                    active_markets=markets,
                    open_positions=open_positions,
                )
                logger.info("Intelligence scan complete: %d scored markets",
                            len(intel_report.scores) if intel_report else 0)
            except Exception as e:
                logger.error("Intelligence scan failed: %s", e, exc_info=True)

        if intel_report is None:
            logger.warning("No intelligence data available — skipping all entries")
            return

        bankroll = self.current_bankroll

        for market in markets:
            try:
                # Skip if we already have a position in this market
                if self.portfolio.has_existing_position(market.id):
                    continue

                # Re-check max positions (may have added during this loop)
                if len(open_positions) >= self.config.MAX_CONCURRENT_POSITIONS:
                    logger.info("Max positions reached (%d) — stopping scan loop",
                                len(open_positions))
                    break

                # Check total exposure across ALL agents (shared bankroll)
                from shared.bankroll import get_total_exposure
                total_exposure = get_total_exposure(self.config.DATA_DIR)

                # NBA cash reserve: always keep $90 available for NBA agent
                available_for_events = bankroll - self.config.NBA_CASH_RESERVE
                max_total = min(
                    bankroll * self.config.MAX_TOTAL_EXPOSURE_PCT,
                    available_for_events,
                )
                if total_exposure >= max_total:
                    logger.info("Exposure limit reached ($%.2f >= $%.2f, NBA reserve=$%.0f)",
                                total_exposure, max_total, self.config.NBA_CASH_RESERVE)
                    break

                # Per-category concentration limit
                cat_key = market.category.value.lower() if hasattr(market.category, 'value') else str(market.category).lower()
                cat_count = sum(
                    1 for p in open_positions
                    if getattr(p, 'category', '') == cat_key
                )
                if cat_count >= self.config.MAX_PER_CATEGORY:
                    logger.debug("Category %s at limit (%d positions) — skipping %s",
                                 cat_key, cat_count, market.slug)
                    continue

                # --- INTELLIGENCE-DRIVEN EDGE EVALUATION ---
                # Get lifecycle and regime for this market
                lifecycle = None
                regime = None
                quality_adjustments = None

                if hasattr(intel_report, 'lifecycle_assessments'):
                    lifecycle = intel_report.lifecycle_assessments.get(market.id)
                if hasattr(intel_report, 'regime_assessments'):
                    regime = intel_report.regime_assessments.get(market.id)
                if hasattr(intel_report, 'quality_adjustments'):
                    quality_adjustments = intel_report.quality_adjustments

                edge_result = await self.analyzer.analyze_with_intelligence(
                    market,
                    intelligence_report=intel_report,
                    lifecycle=lifecycle,
                    regime=regime,
                    quality_adjustments=quality_adjustments,
                )

                if edge_result is None or not edge_result.has_edge:
                    continue

                # --- HARD GATE: refuse to trade without intelligence signals ---
                if edge_result.edge_source not in ("intelligence_blend", "time_decay"):
                    logger.warning(
                        "Blocking trade on %s: no intelligence signals (source=%s)",
                        market.slug, edge_result.edge_source,
                    )
                    continue

                # HARD GATE: composite_score must be > 0 for intelligence_blend
                if edge_result.edge_source == "intelligence_blend":
                    composite = intel_report.scores.get(market.id)
                    composite_val = 0.0
                    if composite is not None:
                        if hasattr(composite, "composite"):
                            composite_val = composite.composite
                        elif isinstance(composite, dict):
                            composite_val = composite.get("composite", 0)
                    if composite_val <= 0:
                        logger.warning(
                            "Blocking trade on %s: composite_score=%.4f (must be > 0)",
                            market.slug, composite_val,
                        )
                        continue

                # Entry price zone filter: only enter if recommended side's
                # price is in the backtest-derived profitable zone
                # NOTE: edge_result.market_price is ALREADY the price of our
                # recommended side (YES price if side=YES, NO price if side=NO)
                recommended_price = edge_result.market_price

                # Duration-aware entry price cap:
                # Short markets (<14d): allow up to 0.85 (high-prob NO bets
                # on things like "Oil hits $110 by Friday" are valid)
                # Default: 0.65 max
                max_entry = self.config.MAX_ENTRY_PRICE  # default 0.65
                if market.end_date:
                    try:
                        from nba_agent.utils import parse_utc
                        remaining = (parse_utc(market.end_date) - utcnow()).total_seconds() / 86400
                        if remaining <= 14:
                            max_entry = 0.85  # short-duration: wider entry zone
                    except Exception:
                        pass

                if recommended_price < self.config.MIN_ENTRY_PRICE or recommended_price > max_entry:
                    logger.info(
                        "Skipping %s: entry price %.2f outside profitable zone [%.2f, %.2f]",
                        market.slug, recommended_price,
                        self.config.MIN_ENTRY_PRICE, max_entry,
                    )
                    continue

                # Apply category priority weight to effective score for ranking
                cat_weight = CATEGORY_PRIORITY.get(market.category.value.lower(), 0.50)

                logger.info(
                    "EDGE FOUND: %s | edge=%.1f%% conf=%s fair=%.2f market=%.2f src=%s cat_wt=%.2f",
                    market.question[:60],
                    edge_result.edge * 100,
                    edge_result.confidence.value,
                    edge_result.our_fair_price,
                    edge_result.market_price,
                    edge_result.edge_source,
                    cat_weight,
                )

                # Calculate bet size — Half Kelly capped at MAX_BET_PCT
                bet_size = self._calculate_bet_size(edge_result, bankroll)
                if bet_size <= 0:
                    logger.debug("Bet size too small for %s", market.question[:40])
                    continue

                # --- MIN BET SIZE GATE ---
                if bet_size < self.config.MIN_BET_SIZE:
                    logger.debug("Bet size $%.2f below minimum $%.2f — skipping %s",
                                 bet_size, self.config.MIN_BET_SIZE, market.question[:40])
                    continue

                # Check remaining exposure room
                remaining_room = max_total - total_exposure
                if bet_size > remaining_room:
                    bet_size = max(0, remaining_room)
                    if bet_size < self.config.MIN_BET_SIZE:
                        logger.info("Not enough exposure room for %s", market.question[:40])
                        continue

                # Execute trade
                position, trade = self.executor.execute_buy(edge_result, bet_size)
                if position and trade:
                    # Record signal attribution at entry time
                    self._record_signal_attribution(position, market)

                    self.portfolio.save_position(position)
                    self.portfolio.log_trade(trade)

                    logger.info(
                        "BET PLACED: %s | $%.2f @ %.2f¢ | edge=%.1f%% | conf=%s | cat=%s",
                        position.market_question[:50],
                        bet_size,
                        position.entry_price * 100,
                        edge_result.edge * 100,
                        edge_result.confidence.value,
                        market.category.value,
                    )

                    open_positions = self.portfolio.get_open_positions()

            except Exception as e:
                logger.error("Error processing market %s: %s", market.id, e, exc_info=True)

    def _calculate_bet_size(self, edge_result, bankroll: float) -> float:
        """Calculate bet size using Half Kelly, capped at MAX_BET_PCT."""
        edge = edge_result.edge
        market_price = edge_result.market_price

        if market_price <= 0 or market_price >= 1:
            return 0.0

        odds_against = (1.0 - market_price) / market_price
        if odds_against <= 0:
            return 0.0

        kelly_fraction = (edge / odds_against) * 0.50  # Half Kelly
        bet_size = bankroll * kelly_fraction

        # Cap at MAX_BET_PCT (2% for events)
        max_bet = bankroll * self.config.MAX_BET_PCT
        bet_size = min(bet_size, max_bet)

        # Floor at $1
        if bet_size < 1.0:
            return 0.0

        return round(bet_size, 2)

    async def _check_exits(self) -> None:
        """Check all open positions using the SmartExitEngine.

        Gathers intelligence context (composite score, direction, lifecycle,
        regime, order-book depth) for each position and passes it to the
        smart exit evaluation.
        """
        open_positions = self.portfolio.get_open_positions()

        # Load intelligence context from disk for exit decisions
        from nba_agent.utils import load_json as _load_json
        intel_data = _load_json(self.config.DATA_DIR / "intelligence_report.json", {})
        scores_list = intel_data.get("scores", [])
        lifecycle_data = intel_data.get("lifecycle_assessments", {})
        regime_data = intel_data.get("regime_assessments", {})

        # Index scores by market_id for O(1) lookup
        scores_by_market: dict[str, dict] = {}
        for s in scores_list:
            mid = s.get("market_id", "")
            if mid:
                scores_by_market[mid] = s

        for position in open_positions:
            try:
                current_price = await self.scanner.get_market_price(position.token_id)
                if current_price is None:
                    continue

                # Gather intelligence context for this position's market
                composite_score = None
                composite_direction = None
                remaining_edge = None
                lifecycle_stage = None
                regime = None
                bid_depth = None

                score_entry = scores_by_market.get(position.market_id)
                if score_entry:
                    composite_score = score_entry.get("composite")
                    composite_direction = score_entry.get("direction")
                    if composite_score is not None and current_price > 0:
                        remaining_edge = max(0, composite_score - current_price)

                # Lifecycle stage
                if isinstance(lifecycle_data, dict):
                    lc = lifecycle_data.get(position.market_id)
                    if isinstance(lc, dict):
                        lifecycle_stage = lc.get("stage")

                # Regime
                if isinstance(regime_data, dict):
                    ra = regime_data.get(position.market_id)
                    if isinstance(ra, dict):
                        regime = ra.get("regime")

                # Get order book depth
                try:
                    book = await self.scanner.get_order_book(position.token_id)
                    if book:
                        bid_depth = sum(float(b[1]) for b in book.get("bids", []))
                except Exception:
                    pass

                # Run smart exit evaluation
                should_exit, reason = self.portfolio.should_exit_early(
                    position=position,
                    current_price=current_price,
                    composite_score=composite_score,
                    composite_direction=composite_direction,
                    remaining_edge=remaining_edge,
                    lifecycle_stage=lifecycle_stage,
                    regime=regime,
                    bid_depth=bid_depth,
                )

                if not should_exit:
                    continue

                logger.info("SMART EXIT: %s | reason=%s", position.market_question[:50], reason)

                trade = self.executor.execute_sell(position, current_price, reason)
                if trade:
                    self.portfolio.save_position(position)
                    self.portfolio.log_trade(trade)

                    logger.info("EXIT: %s | P&L=$%.2f | reason=%s",
                                position.market_question[:50], position.pnl or 0, reason)

            except Exception as e:
                logger.error("Error checking exit for %s: %s", position.id, e)

    async def _check_resolutions(self) -> None:
        """Check for markets that have ended and auto-resolve positions."""
        resolved = self.portfolio.check_resolved_positions()
        for pos in resolved:
            try:
                current_price = await self.scanner.get_market_price(pos.token_id)
                if current_price is None:
                    logger.info("Cannot get price for resolved events position %s — will retry", pos.id)
                    continue

                if current_price >= 0.99:
                    payout = pos.shares * 1.0
                    pnl = payout - pos.cost
                    result = "WIN"
                elif current_price <= 0.01:
                    payout = 0.0
                    pnl = -pos.cost
                    result = "LOSS"
                else:
                    continue

                pos.status = "closed"
                pos.exit_price = current_price
                pos.exit_time = utcnow().isoformat()
                pos.pnl = round(pnl, 2)
                pos.exit_reason = f"Market resolved: {result}"
                self.portfolio.save_position(pos)

                logger.info("RESOLVED: %s | %s | P&L=$%.2f",
                            pos.market_question[:50], result, pnl)

            except Exception as e:
                logger.error("Error resolving events position %s: %s", pos.id, e)

    def _record_signal_attribution(self, position, market) -> None:
        """Record which intelligence signals drove a trade entry.

        Captures the composite score, contributing signals, regime, and
        lifecycle at the time of entry. This data is saved with the position
        and never reconstructed later.
        """
        try:
            from intelligence.manager import IntelligenceManager
            from nba_agent.utils import load_json

            # Try to read the latest intelligence report from disk
            report_path = self.config.DATA_DIR / "intelligence_report.json"
            report_data = load_json(report_path, {})

            # Find composite score for this market
            scores = report_data.get("scores", [])
            for score_entry in scores:
                mid = score_entry.get("market_id", "")
                if mid == market.id:
                    position.entry_composite = score_entry.get("composite", 0.0)
                    position.last_composite = position.entry_composite

                    # Build entry_signals from signal breakdown
                    breakdown = score_entry.get("signal_breakdown", {})
                    entry_signals = []
                    for source, info in breakdown.items():
                        entry_signals.append({
                            "source": source,
                            "strength": info.get("strength", 0),
                            "direction": info.get("direction", "NEUTRAL"),
                        })
                    position.entry_signals = entry_signals
                    break

            # Lifecycle at entry
            lifecycles = report_data.get("lifecycle_assessments", {})
            if isinstance(lifecycles, dict):
                lc = lifecycles.get(market.id, {})
                if isinstance(lc, dict):
                    position.lifecycle_at_entry = lc.get("stage", "")
                elif hasattr(lc, "stage"):
                    position.lifecycle_at_entry = lc.stage

            # Regime at entry
            regimes = report_data.get("regime_assessments", {})
            if isinstance(regimes, dict):
                ra = regimes.get(market.id, {})
                if isinstance(ra, dict):
                    position.regime_at_entry = ra.get("regime", "")
                elif hasattr(ra, "regime"):
                    position.regime_at_entry = ra.regime

            # Initialize peak tracking
            position.peak_pnl_pct = 0.0
            position.peak_price = position.entry_price

            logger.info(
                "Signal attribution: composite=%.3f signals=%d regime=%s lifecycle=%s",
                position.entry_composite,
                len(position.entry_signals or []),
                position.regime_at_entry,
                position.lifecycle_at_entry,
            )

        except Exception as e:
            logger.warning("Could not record signal attribution: %s", e)
            position.entry_signals = []
            position.entry_composite = 0.0
            position.peak_pnl_pct = 0.0
            position.peak_price = position.entry_price

    def shutdown(self) -> None:
        """Signal graceful shutdown."""
        logger.info("Events Agent shutdown requested")
        self._shutdown = True


def setup_logging(level: str) -> None:
    """Configure structured logging."""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=log_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    """Entry point for `python -m events_agent`."""
    config = EventsConfig()
    setup_logging(config.LOG_LEVEL)

    agent = EventsAgent()

    def handle_signal(sig, frame):
        agent.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        agent.shutdown()
        logger.info("Events Agent stopped by keyboard interrupt")


if __name__ == "__main__":
    main()
