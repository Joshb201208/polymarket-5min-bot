"""IntelligenceManager — orchestrates all intelligence modules in parallel.

Central entry point for the intelligence system. Runs all scanners concurrently,
collects signals, computes composite scores, and returns a unified IntelligenceReport.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from intelligence.models import (
    Signal, CompositeScore, CorrelationReport, IntelligenceReport,
)
from intelligence import config as intel_config

logger = logging.getLogger(__name__)

# Data directory
_project_root = Path(__file__).resolve().parent.parent
DATA_DIR = Path("/root/polymarket-bot/data") if Path("/root/polymarket-bot/data").exists() else _project_root / "data"


class IntelligenceManager:
    """Central orchestration class that runs all intelligence modules."""

    def __init__(self, config=None):
        self.config = config
        self.all_signals: list[Signal] = []
        self._source_health: dict = {}
        self._modules = {}
        self._volume_rotation_task: asyncio.Task | None = None
        self._init_modules()

    def _init_modules(self):
        """Initialize enabled intelligence modules (lazy imports)."""
        modules = intel_config.INTELLIGENCE_MODULES

        for name, enabled in modules.items():
            if enabled:
                self._source_health[name] = {
                    "status": "initialized",
                    "last_update": None,
                    "error": None,
                }
            else:
                self._source_health[name] = {
                    "status": "disabled",
                    "last_update": None,
                    "error": None,
                }

        # Try to import and instantiate each module
        if modules.get("x_scanner"):
            try:
                from intelligence.x_scanner import XScanner
                self._modules["x_scanner"] = XScanner()
            except ImportError:
                logger.warning("x_scanner module not available yet")

        if modules.get("orderbook"):
            try:
                from intelligence.orderbook import OrderbookIntelligence
                self._modules["orderbook"] = OrderbookIntelligence()
            except ImportError:
                logger.warning("orderbook module not available yet")

        if modules.get("metaculus"):
            try:
                from intelligence.metaculus import MetaculusCompare
                self._modules["metaculus"] = MetaculusCompare()
            except ImportError:
                logger.warning("metaculus module not available yet")

        if modules.get("google_trends"):
            try:
                from intelligence.google_trends import GoogleTrendsTracker
                self._modules["google_trends"] = GoogleTrendsTracker()
            except ImportError:
                logger.warning("google_trends module not available yet")

        if modules.get("congress"):
            try:
                from intelligence.congress_tracker import CongressTracker
                self._modules["congress"] = CongressTracker()
            except ImportError:
                logger.warning("congress_tracker module not available yet")

        if modules.get("cross_market"):
            try:
                from intelligence.cross_market import CrossMarketArbitrage
                self._modules["cross_market"] = CrossMarketArbitrage()
            except ImportError:
                logger.warning("cross_market module not available yet")

        if modules.get("whale_tracker"):
            try:
                from intelligence.whale_tracker import WhaleTracker
                self._modules["whale_tracker"] = WhaleTracker()
            except ImportError:
                logger.warning("whale_tracker module not available yet")

        try:
            from intelligence.composite_scorer import CompositeScorer
            self._composite_scorer = CompositeScorer()
        except ImportError:
            logger.warning("composite_scorer module not available yet")
            self._composite_scorer = None

        try:
            from intelligence.correlation import CorrelationMonitor
            self._correlation = CorrelationMonitor()
        except ImportError:
            logger.warning("correlation module not available yet")
            self._correlation = None

    async def start_persistent(self, token_ids: list[str] | None = None):
        """Start long-running tasks (WebSocket, volume rotation, etc.).

        Call once at orchestrator startup. Runs in background.
        """
        orderbook = self._modules.get("orderbook")
        if orderbook and hasattr(orderbook, "connect"):
            try:
                await orderbook.connect(token_ids or [])
                asyncio.create_task(orderbook.run())
                self._source_health["orderbook"]["status"] = "connected"
                logger.info("Orderbook WebSocket started")
            except Exception as e:
                logger.error("Failed to start orderbook WebSocket: %s", e)
                self._source_health["orderbook"]["status"] = "error"
                self._source_health["orderbook"]["error"] = str(e)

        # Start volume window rotation (every 15 min)
        self._volume_rotation_task = asyncio.create_task(
            self._rotate_volume_windows()
        )

    async def run_scan_cycle(
        self,
        active_markets: list,
        open_positions: list | None = None,
    ) -> IntelligenceReport:
        """Run all scanners in parallel, combine results into IntelligenceReport.

        Args:
            active_markets: List of EventMarket objects to scan.
            open_positions: List of Position objects for correlation analysis.

        Returns:
            IntelligenceReport with all signals, scores, and correlation data.
        """
        open_positions = open_positions or []
        now = datetime.now(timezone.utc).isoformat()

        logger.info("Intelligence scan cycle starting (%d markets)", len(active_markets))

        # Build scan tasks for all available modules
        scan_tasks = []
        scan_names = []

        for name, module in self._modules.items():
            if hasattr(module, "scan"):
                scan_tasks.append(
                    asyncio.wait_for(module.scan(active_markets), timeout=30)
                )
                scan_names.append(name)

        # Run all scanners concurrently
        results = await asyncio.gather(*scan_tasks, return_exceptions=True)

        # Collect signals, update health
        all_signals: list[Signal] = []
        for name, result in zip(scan_names, results):
            if isinstance(result, Exception):
                logger.error("Scanner %s failed: %s", name, result)
                self._source_health[name]["status"] = "error"
                self._source_health[name]["error"] = str(result)[:200]
            elif isinstance(result, list):
                all_signals.extend(result)
                self._source_health[name]["status"] = "active"
                self._source_health[name]["last_update"] = now
                self._source_health[name]["error"] = None
                logger.info("Scanner %s returned %d signals", name, len(result))
            else:
                self._source_health[name]["status"] = "no_data"
                self._source_health[name]["last_update"] = now

        # Add orderbook signals if persistent connection is running
        orderbook = self._modules.get("orderbook")
        if orderbook and hasattr(orderbook, "get_pending_signals"):
            try:
                ob_signals = orderbook.get_pending_signals()
                all_signals.extend(ob_signals)
                if ob_signals:
                    self._source_health["orderbook"]["last_update"] = now
                    logger.info("Orderbook produced %d signals", len(ob_signals))
            except Exception as e:
                logger.error("Orderbook signal fetch failed: %s", e)

        # Update rolling buffer
        self.all_signals = all_signals

        # Score each market that has signals
        market_scores: dict[str, CompositeScore] = {}
        if self._composite_scorer:
            for market in active_markets:
                market_id = getattr(market, "id", str(market))
                market_signals = [s for s in all_signals if s.market_id == market_id]
                if market_signals:
                    market_scores[market_id] = self._composite_scorer.score(
                        market_id, market_signals
                    )

        # Correlation analysis
        correlation_report = CorrelationReport()
        if self._correlation and open_positions:
            try:
                correlation_report = self._correlation.analyze(open_positions)
            except Exception as e:
                logger.error("Correlation analysis failed: %s", e)

        # Persist for dashboard
        self._save_signals(all_signals)
        self._save_scores(market_scores)
        self._save_health()

        logger.info(
            "Intelligence scan complete: %d signals, %d scored markets",
            len(all_signals),
            len(market_scores),
        )

        return IntelligenceReport(
            signals=all_signals,
            scores=market_scores,
            correlation=correlation_report,
            timestamp=now,
            source_health=dict(self._source_health),
        )

    def get_health(self) -> dict:
        """Return current source health status."""
        return dict(self._source_health)

    async def _rotate_volume_windows(self) -> None:
        """Rotate orderbook volume windows every 15 minutes."""
        while True:
            try:
                await asyncio.sleep(900)  # 15 minutes
                orderbook = self._modules.get("orderbook")
                if orderbook and hasattr(orderbook, "rotate_volume_window"):
                    orderbook.rotate_volume_window()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Volume rotation error: %s", e)

    def _save_signals(self, signals: list[Signal]):
        """Save signals to JSON for dashboard consumption."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = DATA_DIR / "intelligence_signals.json"

            # Load existing and append (keep last 24h)
            existing = []
            if path.exists():
                try:
                    existing = json.loads(path.read_text()).get("signals", [])
                except (json.JSONDecodeError, OSError):
                    existing = []

            # Add new signals
            new_signals = [s.to_dict() if hasattr(s, "to_dict") else s for s in signals]
            all_sigs = existing + new_signals

            # Trim to last 24 hours
            cutoff = datetime.now(timezone.utc).timestamp() - 86400
            trimmed = []
            for sig in all_sigs:
                try:
                    ts = sig.get("timestamp", "")
                    sig_time = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    if sig_time > cutoff:
                        trimmed.append(sig)
                except (ValueError, AttributeError):
                    trimmed.append(sig)  # Keep if we can't parse

            path.write_text(json.dumps({"signals": trimmed}, indent=2, default=str))
        except Exception as e:
            logger.error("Failed to save signals: %s", e)

    def _save_scores(self, scores: dict[str, CompositeScore]):
        """Save composite scores for dashboard."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = DATA_DIR / "intelligence_scores.json"
            data = {
                k: v.to_dict() if hasattr(v, "to_dict") else v
                for k, v in scores.items()
            }
            path.write_text(json.dumps({"scores": data}, indent=2, default=str))
        except Exception as e:
            logger.error("Failed to save scores: %s", e)

    def _save_health(self):
        """Save source health for dashboard."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = DATA_DIR / "intelligence_health.json"
            path.write_text(json.dumps(self._source_health, indent=2, default=str))
        except Exception as e:
            logger.error("Failed to save health: %s", e)

    async def shutdown(self) -> None:
        """Gracefully shut down all persistent tasks."""
        logger.info("Intelligence manager shutting down")
        if self._volume_rotation_task:
            self._volume_rotation_task.cancel()
            try:
                await self._volume_rotation_task
            except asyncio.CancelledError:
                pass
        orderbook = self._modules.get("orderbook")
        if orderbook and hasattr(orderbook, "shutdown"):
            await orderbook.shutdown()
