"""IntelligenceManager — orchestrates all intelligence modules in parallel.

Central entry point for the intelligence system. Runs all scanners concurrently,
collects signals, computes composite scores, and returns a unified IntelligenceReport.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from intelligence.config import IntelligenceConfig
from intelligence.models import CompositeScore, IntelligenceReport, Signal
from intelligence.x_scanner import XScanner
from intelligence.orderbook import OrderbookIntelligence
from intelligence.metaculus import MetaculusCompare
from intelligence.google_trends import GoogleTrendsTracker
from intelligence.congress_tracker import CongressTracker
from intelligence.cross_market import CrossMarketArbitrage
from intelligence.whale_tracker import WhaleTracker
from intelligence.composite_scorer import CompositeScorer
from intelligence.correlation import CorrelationMonitor
from nba_agent.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.manager")


class IntelligenceManager:
    """Orchestrates all intelligence modules and produces unified reports."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self.config.ensure_data_dir()

        self.x_scanner = XScanner(self.config)
        self.orderbook = OrderbookIntelligence(self.config)
        self.metaculus = MetaculusCompare(self.config)
        self.google_trends = GoogleTrendsTracker(self.config)
        self.congress = CongressTracker(self.config)
        self.cross_market = CrossMarketArbitrage(self.config)
        self.whale_tracker = WhaleTracker(self.config)
        self.composite_scorer = CompositeScorer()
        self.correlation = CorrelationMonitor()

        # Rolling buffer of recent signals
        self.all_signals: list[Signal] = []

        # Persistence paths
        self._signals_path = self.config.DATA_DIR / "intelligence_signals.json"
        self._scores_path = self.config.DATA_DIR / "intelligence_scores.json"

        # Volume rotation task handle
        self._volume_rotation_task: asyncio.Task | None = None

    async def start_persistent(self, token_ids: list[str] | None = None) -> None:
        """Start long-running tasks (WebSocket, volume rotation, etc.).

        Call once at orchestrator startup. Runs in background.
        """
        if token_ids and self.config.is_enabled("orderbook"):
            try:
                await self.orderbook.connect(token_ids)
                asyncio.create_task(self.orderbook.run())
                logger.info("Orderbook WebSocket task started")
            except Exception as e:
                logger.error("Failed to start orderbook WS: %s", e)

        # Start volume window rotation (every 15 min)
        self._volume_rotation_task = asyncio.create_task(
            self._rotate_volume_windows()
        )

    async def run_scan_cycle(
        self,
        active_markets: list,
        open_positions: list,
    ) -> IntelligenceReport:
        """Run all scanners in parallel, combine results into IntelligenceReport.

        Args:
            active_markets: List of EventMarket objects to scan.
            open_positions: List of Position objects for correlation analysis.

        Returns:
            IntelligenceReport with all signals, scores, and correlation data.
        """
        now = utcnow()
        logger.info("Intelligence scan cycle starting (%d markets)", len(active_markets))

        # Run all scanners concurrently with individual error handling
        scan_tasks = []

        if self.config.is_enabled("x_scanner"):
            scan_tasks.append(("x_scanner", self.x_scanner.scan(active_markets)))
        if self.config.is_enabled("metaculus"):
            scan_tasks.append(("metaculus", self.metaculus.scan(active_markets)))
        if self.config.is_enabled("google_trends"):
            scan_tasks.append(("google_trends", self.google_trends.scan(active_markets)))
        if self.config.is_enabled("congress"):
            scan_tasks.append(("congress", self.congress.scan(active_markets)))
        if self.config.is_enabled("cross_market"):
            scan_tasks.append(("cross_market", self.cross_market.scan(active_markets)))
        if self.config.is_enabled("whale_tracker"):
            scan_tasks.append(("whale_tracker", self.whale_tracker.scan(active_markets)))

        # Execute concurrently
        results = await asyncio.gather(
            *(task for _, task in scan_tasks),
            return_exceptions=True,
        )

        # Collect all signals (skip failed scanners gracefully)
        all_signals: list[Signal] = []
        for i, result in enumerate(results):
            name = scan_tasks[i][0] if i < len(scan_tasks) else "unknown"
            if isinstance(result, Exception):
                logger.error("Scanner %s failed: %s", name, result)
                continue
            if isinstance(result, list):
                all_signals.extend(result)
                logger.info("Scanner %s returned %d signals", name, len(result))

        # Add orderbook signals from persistent WebSocket
        if self.config.is_enabled("orderbook"):
            orderbook_signals = self.orderbook.get_pending_signals()
            all_signals.extend(orderbook_signals)
            if orderbook_signals:
                logger.info("Orderbook produced %d signals", len(orderbook_signals))

        # Update rolling buffer
        self.all_signals = all_signals

        # Score each market that has signals
        market_scores: dict[str, CompositeScore] = {}
        for market in active_markets:
            market_id = getattr(market, "id", "")
            market_signals = [s for s in all_signals if s.market_id == market_id]
            if market_signals:
                market_scores[market_id] = self.composite_scorer.score(
                    market_id, market_signals,
                )

        # Correlation analysis on open positions
        correlation_report = None
        if open_positions:
            try:
                correlation_report = self.correlation.analyze(open_positions)
            except Exception as e:
                logger.error("Correlation analysis failed: %s", e)

        # Persist for dashboard
        self._save_signals(all_signals)
        self._save_scores(market_scores)

        report = IntelligenceReport(
            signals=all_signals,
            scores=market_scores,
            correlation=correlation_report,
            timestamp=now,
        )

        logger.info(
            "Intelligence scan complete: %d signals, %d scored markets",
            len(all_signals),
            len(market_scores),
        )
        return report

    async def _rotate_volume_windows(self) -> None:
        """Rotate orderbook volume windows every 15 minutes."""
        while True:
            try:
                await asyncio.sleep(900)  # 15 minutes
                self.orderbook.rotate_volume_window()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Volume rotation error: %s", e)

    def _save_signals(self, signals: list[Signal]) -> None:
        """Persist signals for dashboard consumption."""
        if not signals:
            return
        try:
            data = [s.to_dict() for s in signals]
            atomic_json_write(self._signals_path, data)
        except Exception as e:
            logger.warning("Failed to save intelligence signals: %s", e)

    def _save_scores(self, scores: dict[str, CompositeScore]) -> None:
        """Persist composite scores for dashboard consumption."""
        if not scores:
            return
        try:
            data = {
                market_id: score.to_dict()
                for market_id, score in scores.items()
            }
            atomic_json_write(self._scores_path, data)
        except Exception as e:
            logger.warning("Failed to save intelligence scores: %s", e)

    async def shutdown(self) -> None:
        """Gracefully shut down all persistent tasks."""
        logger.info("Intelligence manager shutting down")
        if self._volume_rotation_task:
            self._volume_rotation_task.cancel()
            try:
                await self._volume_rotation_task
            except asyncio.CancelledError:
                pass
        await self.orderbook.shutdown()
