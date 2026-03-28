"""Telegram command interface for the Events agent.

Provides operational control via Telegram commands that coexist
with existing NBA/NHL commands.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from events_agent.config import EventsConfig
from nba_agent.utils import load_json, atomic_json_write, utcnow

logger = logging.getLogger(__name__)


class EventsTelegramCommands:
    """Handle Telegram commands for the events agent."""

    COMMANDS = {
        "/events": "Show current events positions and P&L",
        "/signals": "Show top 5 active intelligence signals",
        "/health": "Intelligence module health check",
        "/exposure": "Cross-agent bankroll exposure report",
        "/override": "Manually force a position: /override <slug> YES|NO <amount>",
        "/calibration": "Show current calibrator weights and accuracy",
        "/regime": "Show detected market regimes for active positions",
        "/lifecycle": "Show lifecycle stages for active markets",
        "/kill": "Emergency: close all events positions immediately",
        "/pause_events": "Pause events agent scanning",
        "/resume_events": "Resume events agent scanning",
    }

    def __init__(self, config: EventsConfig | None = None) -> None:
        self.config = config or EventsConfig()
        self.token = self.config.TELEGRAM_BOT_TOKEN
        self.chat_id = self.config.TELEGRAM_CHAT_ID
        self.api_base = self.config.TELEGRAM_API_BASE
        self._data_dir = self.config.DATA_DIR

        # References to agent subsystems — set by orchestrator
        self.portfolio = None
        self.scanner = None
        self.executor = None
        self.smart_executor = None
        self.intelligence = None
        self.lifecycle = None
        self.regime_detector = None
        self.calibrator = None
        self.live_quality = None
        self.dedup = None

        # Pause flag path
        self._pause_flag_path = self._data_dir / "events_paused.json"

    @property
    def is_paused(self) -> bool:
        data = load_json(self._pause_flag_path, {})
        return data.get("paused", False)

    def set_paused(self, paused: bool) -> None:
        atomic_json_write(self._pause_flag_path, {
            "paused": paused,
            "updated_at": utcnow().isoformat(),
        })

    def is_events_command(self, text: str) -> bool:
        """Check if a message is an events agent command."""
        if not text:
            return False
        cmd = text.strip().split()[0].lower()
        return cmd in self.COMMANDS

    async def handle_command(self, text: str) -> str:
        """Process a Telegram command and return response text."""
        parts = text.strip().split()
        command = parts[0].lower()
        args = parts[1:]

        try:
            if command == "/events":
                return self._format_positions_report()
            elif command == "/signals":
                return self._format_top_signals()
            elif command == "/health":
                return self._format_health_report()
            elif command == "/exposure":
                return self._format_exposure_report()
            elif command == "/override":
                return await self._handle_override(args)
            elif command == "/calibration":
                return self._format_calibration_report()
            elif command == "/regime":
                return self._format_regime_report()
            elif command == "/lifecycle":
                return self._format_lifecycle_report()
            elif command == "/kill":
                return await self._emergency_close_all()
            elif command == "/pause_events":
                self.set_paused(True)
                return "Events agent scanning paused."
            elif command == "/resume_events":
                self.set_paused(False)
                return "Events agent scanning resumed."
            else:
                return f"Unknown command: {command}"
        except Exception as e:
            logger.error("Error handling command %s: %s", command, e, exc_info=True)
            return f"Error: {e}"

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------

    def _format_positions_report(self) -> str:
        """Format current events positions for /events command."""
        positions_data = load_json(self._data_dir / "events_positions.json", {"positions": []})
        positions = positions_data.get("positions", [])
        open_pos = [p for p in positions if p.get("status") == "open"]

        if not open_pos:
            return "<b>Events Positions</b>\n\nNo open positions."

        # Calculate total P&L from closed
        closed = [p for p in positions if p.get("status") != "open"]
        total_pnl = sum(p.get("pnl", 0) or 0 for p in closed)
        total_exposure = sum(p.get("cost", 0) or 0 for p in open_pos)

        lines = [f"<b>Events Positions ({len(open_pos)} open)</b>\n"]

        for p in open_pos:
            entry = p.get("entry_price", 0)
            cost = p.get("cost", 0)
            side = p.get("side", "?")
            question = (p.get("market_question") or "")[:55]
            edge = p.get("edge_at_entry", 0) or 0
            category = p.get("category", "other")

            lines.append(
                f"  {question}\n"
                f"  {side} @ {entry*100:.0f}c | ${cost:.2f} | edge {edge*100:.1f}% | {category}"
            )

        lines.append(f"\nExposure: ${total_exposure:.2f}")
        lines.append(f"Realized P&L: ${total_pnl:+.2f}")

        return "\n".join(lines)

    def _format_top_signals(self) -> str:
        """Format top 5 intelligence signals for /signals command."""
        report_data = load_json(self._data_dir / "intelligence_report.json", {})
        signals = report_data.get("signals", [])

        if not signals:
            return "<b>Active Signals</b>\n\nNo active signals."

        # Sort by strength * confidence descending
        scored = []
        for s in signals:
            strength = s.get("strength", 0)
            confidence = s.get("confidence", 0)
            scored.append((s, strength * confidence))
        scored.sort(key=lambda x: x[1], reverse=True)

        lines = ["<b>Top 5 Active Signals</b>\n"]
        for s, score in scored[:5]:
            source = s.get("source", "?")
            direction = s.get("direction", "?")
            strength = s.get("strength", 0)
            question = (s.get("market_question") or "")[:45]
            emoji = "+" if direction == "YES" else "-" if direction == "NO" else "~"

            lines.append(
                f"  [{source}] {emoji}{direction} {strength*100:.0f}%\n"
                f"  {question}"
            )

        return "\n".join(lines)

    def _format_health_report(self) -> str:
        """Format intelligence module health for /health command."""
        report_data = load_json(self._data_dir / "intelligence_report.json", {})
        health = report_data.get("source_health", {})

        # Also check live quality if available
        quality_data = load_json(self._data_dir / "live_quality_log.json", {})

        if not health:
            return "<b>Intelligence Health</b>\n\nNo health data available."

        lines = ["<b>Intelligence Module Health</b>\n"]
        for source, info in sorted(health.items()):
            status = info.get("status", "unknown")
            last = info.get("last_update", "--")
            error = info.get("error")

            icon = "+" if status in ("ok", "initialized") else "-" if error else "~"
            line = f"  {icon} {source}: {status}"
            if last and last != "--":
                # Show relative time
                try:
                    ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    ago = (utcnow() - ts).total_seconds() / 60
                    line += f" ({ago:.0f}m ago)"
                except (ValueError, TypeError):
                    pass
            if error:
                line += f"\n    Error: {str(error)[:60]}"
            lines.append(line)

        return "\n".join(lines)

    def _format_exposure_report(self) -> str:
        """Format cross-agent exposure for /exposure command."""
        from shared.bankroll import get_agent_exposure, get_total_exposure

        bankroll_data = load_json(self._data_dir / "bankroll.json", {})
        bankroll = bankroll_data.get("current_bankroll", 0)

        nba_exp = get_agent_exposure(self._data_dir, "nba")
        events_exp = get_agent_exposure(self._data_dir, "events")
        total_exp = get_total_exposure(self._data_dir)
        max_exp = bankroll * self.config.MAX_TOTAL_EXPOSURE_PCT

        nba_pct = (nba_exp / bankroll * 100) if bankroll > 0 else 0
        events_pct = (events_exp / bankroll * 100) if bankroll > 0 else 0
        total_pct = (total_exp / bankroll * 100) if bankroll > 0 else 0

        return (
            f"<b>Cross-Agent Exposure</b>\n\n"
            f"  NBA:    ${nba_exp:.2f} ({nba_pct:.1f}%)\n"
            f"  Events: ${events_exp:.2f} ({events_pct:.1f}%)\n"
            f"  ────────────────\n"
            f"  Total:  ${total_exp:.2f} ({total_pct:.1f}%)\n"
            f"  Max:    ${max_exp:.2f} (50%)\n\n"
            f"  Bankroll: ${bankroll:.2f}\n"
            f"  Room: ${max(0, max_exp - total_exp):.2f}"
        )

    async def _handle_override(self, args: list) -> str:
        """Handle /override <slug> YES|NO <amount> command.

        Bypasses edge checks but respects bankroll limits.
        """
        if len(args) < 3:
            return "Usage: /override <market_slug> YES|NO <amount>"

        slug = args[0]
        direction = args[1].upper()
        try:
            amount = float(args[2].replace("$", ""))
        except ValueError:
            return "Invalid amount. Usage: /override <slug> YES|NO <amount>"

        if direction not in ("YES", "NO"):
            return "Direction must be YES or NO."

        if amount < 1:
            return "Minimum override amount is $1."

        # Check bankroll limit
        from shared.bankroll import get_total_exposure
        bankroll_data = load_json(self._data_dir / "bankroll.json", {})
        bankroll = bankroll_data.get("current_bankroll", 0)
        total_exp = get_total_exposure(self._data_dir)
        max_exp = bankroll * self.config.MAX_TOTAL_EXPOSURE_PCT

        if total_exp + amount > max_exp:
            return (
                f"Override rejected: ${amount:.2f} would exceed 50% exposure limit.\n"
                f"Current: ${total_exp:.2f} / ${max_exp:.2f}"
            )

        # Log the override
        overrides_path = self._data_dir / "manual_overrides.json"
        overrides = load_json(overrides_path, {"overrides": []})
        overrides["overrides"].append({
            "slug": slug,
            "direction": direction,
            "amount": amount,
            "timestamp": utcnow().isoformat(),
            "status": "pending",
        })
        atomic_json_write(overrides_path, overrides)

        return (
            f"Override logged: {direction} on {slug} for ${amount:.2f}\n"
            f"Will be executed on next scan cycle."
        )

    def _format_calibration_report(self) -> str:
        """Format calibration data for /calibration command."""
        cal_data = load_json(self._data_dir / "calibration_history.json", {})

        if not cal_data or not cal_data.get("entries"):
            return "<b>Calibration</b>\n\nNo calibration data yet. Need 30+ resolved trades."

        latest = cal_data["entries"][-1] if cal_data.get("entries") else {}
        weights = latest.get("weights", {})
        accuracy = latest.get("source_accuracy", {})

        default_weights = {
            "metaculus": 0.25, "x_scanner": 0.20, "orderbook": 0.15,
            "whale_tracker": 0.15, "google_trends": 0.10, "congress": 0.08,
            "cross_market": 0.07,
        }

        lines = ["<b>Calibration Report</b>\n"]
        lines.append("Source          Default  Current  Accuracy")
        lines.append("──────────────────────────────────────")

        for source in default_weights:
            dw = default_weights.get(source, 0)
            cw = weights.get(source, dw)
            acc = accuracy.get(source, {}).get("accuracy", 0)
            arrow = "+" if cw > dw else "-" if cw < dw else "="
            lines.append(f"  {source:<14} {dw:.2f}    {cw:.2f}{arrow}   {acc*100:.0f}%")

        return "\n".join(lines)

    def _format_regime_report(self) -> str:
        """Format regime detection for /regime command."""
        regime_data = load_json(self._data_dir / "regime_assessments.json", {})
        assessments = regime_data.get("assessments", {})

        if not assessments:
            return "<b>Market Regimes</b>\n\nNo regime data available."

        lines = ["<b>Market Regimes</b>\n"]
        for market_id, info in list(assessments.items())[:10]:
            regime = info.get("regime", "?")
            vol = info.get("volatility", 0)
            rec = info.get("recommendation", "?")
            question = (info.get("market_question") or market_id)[:45]

            emoji_map = {
                "trending": ">>", "volatile": "!!", "stale": "--", "converging": "><"
            }
            emoji = emoji_map.get(regime, "??")

            lines.append(f"  {emoji} {question}\n     {regime} | vol {vol*100:.1f}% | {rec}")

        return "\n".join(lines)

    def _format_lifecycle_report(self) -> str:
        """Format lifecycle stages for /lifecycle command."""
        lifecycle_data = load_json(self._data_dir / "lifecycle_assessments.json", {})
        assessments = lifecycle_data.get("assessments", {})

        if not assessments:
            return "<b>Lifecycle Stages</b>\n\nNo lifecycle data available."

        lines = ["<b>Lifecycle Stages</b>\n"]
        for market_id, info in list(assessments.items())[:10]:
            stage = info.get("stage", "?")
            days = info.get("days_remaining", 0)
            min_edge = info.get("min_edge", 0)
            strategy = info.get("hold_strategy", "?")
            question = (info.get("market_question") or market_id)[:45]

            stage_emoji = {
                "early": "[E]", "developing": "[D]", "mature": "[M]",
                "late": "[L]", "terminal": "[T]", "unknown": "[?]",
            }
            emoji = stage_emoji.get(stage, "[?]")

            lines.append(
                f"  {emoji} {question}\n"
                f"     {stage} | {days:.0f}d left | edge>{min_edge*100:.0f}% | {strategy}"
            )

        return "\n".join(lines)

    async def _emergency_close_all(self) -> str:
        """Emergency: close all events positions at market price.

        1. Market-sell all events positions
        2. Pause the events agent
        3. Log the kill event
        """
        from events_agent.executor import EventsExecutor

        executor = EventsExecutor(self.config)
        positions_data = load_json(self._data_dir / "events_positions.json", {"positions": []})
        positions = positions_data.get("positions", [])
        open_pos = [p for p in positions if p.get("status") == "open"]

        if not open_pos:
            self.set_paused(True)
            return "KILL: No open events positions. Agent paused."

        total_pnl = 0.0
        closed_count = 0

        for p_dict in open_pos:
            try:
                from events_agent.models import Position
                pos = Position.from_dict(p_dict)

                # Get current price (use fallback if scanner unavailable)
                current_price = 0.50
                if self.scanner:
                    try:
                        price = await self.scanner.get_market_price(pos.token_id)
                        if price is not None:
                            current_price = price
                    except Exception:
                        pass

                trade = executor.execute_sell(pos, current_price, "EMERGENCY KILL")
                if trade:
                    # Update position in the list
                    for i, existing in enumerate(positions):
                        if existing.get("id") == pos.id:
                            positions[i] = pos.to_dict()
                            break
                    total_pnl += pos.pnl or 0
                    closed_count += 1
            except Exception as e:
                logger.error("Kill: failed to close %s: %s", p_dict.get("id"), e)

        # Save updated positions
        atomic_json_write(
            self._data_dir / "events_positions.json",
            {"positions": [p if isinstance(p, dict) else p.to_dict() for p in positions]},
        )

        # Pause agent
        self.set_paused(True)

        # Log kill event
        kills_path = self._data_dir / "emergency_kills.json"
        kills = load_json(kills_path, {"kills": []})
        kills["kills"].append({
            "timestamp": utcnow().isoformat(),
            "positions_closed": closed_count,
            "total_pnl": round(total_pnl, 2),
        })
        atomic_json_write(kills_path, kills)

        return (
            f"KILL EXECUTED\n\n"
            f"Closed {closed_count} positions\n"
            f"Total P&L: ${total_pnl:+.2f}\n"
            f"Agent PAUSED.\n\n"
            f"Use /resume_events to restart."
        )

    # ------------------------------------------------------------------
    # Telegram messaging
    # ------------------------------------------------------------------

    async def send_message(self, text: str) -> bool:
        """Send a message to the configured Telegram chat."""
        if not self.token or not self.chat_id:
            logger.warning("Telegram not configured — skipping events command response")
            return False

        url = f"{self.api_base}/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return True
                logger.error("Telegram send failed: %d %s", resp.status_code, resp.text)
                return False
        except Exception as e:
            logger.error("Telegram send error: %s", e)
            return False

    async def poll_and_handle(self) -> None:
        """Poll for new Telegram messages and handle events commands.

        This should be called periodically from the orchestrator.
        Handles only events-agent commands, ignores everything else.
        """
        if not self.token or not self.chat_id:
            return

        offset_path = self._data_dir / "telegram_offset.json"
        offset_data = load_json(offset_path, {})
        update_offset = offset_data.get("offset", 0)

        url = f"{self.api_base}/bot{self.token}/getUpdates"
        params = {"offset": update_offset, "timeout": 1, "limit": 10}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    return

                data = resp.json()
                results = data.get("result", [])

                for update in results:
                    update_id = update.get("update_id", 0)
                    update_offset = max(update_offset, update_id + 1)

                    message = update.get("message", {})
                    text = message.get("text", "").strip()
                    chat_id = str(message.get("chat", {}).get("id", ""))

                    # Only respond to messages from our configured chat
                    if chat_id != self.chat_id:
                        continue

                    if self.is_events_command(text):
                        response = await self.handle_command(text)
                        await self.send_message(response)

                # Save offset
                atomic_json_write(offset_path, {"offset": update_offset})

        except Exception as e:
            logger.debug("Telegram poll error: %s", e)
