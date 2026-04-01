"""Macro Conditions Monitor — tracks market regime and signals deployment modes.

Fetches SPX, VIX, crude oil (USO proxy), and treasury (TLT proxy) data
to compute a market regime score. The regime determines how aggressively
the bot scans and deploys capital.

Regimes:
    AGGRESSIVE_DEPLOY — Market dip into support + fear spike = back up the truck
    DIP_OPPORTUNITY   — Conditions forming for a buy-the-dip opportunity
    NORMAL            — Business as usual
    CAUTIOUS          — Elevated risk, tighten stops, reduce new entries
"""

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from stock_agent.config import Config

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/stable"


# ── Regime definitions ───────────────────────────────────────────────

REGIME_AGGRESSIVE_DEPLOY = "AGGRESSIVE_DEPLOY"
REGIME_DIP_OPPORTUNITY = "DIP_OPPORTUNITY"
REGIME_NORMAL = "NORMAL"
REGIME_CAUTIOUS = "CAUTIOUS"

REGIME_DESCRIPTIONS = {
    REGIME_AGGRESSIVE_DEPLOY: "Market dip into support with fear spike — deploy capital aggressively",
    REGIME_DIP_OPPORTUNITY: "Conditions forming for buy-the-dip — widen scanning, prepare to deploy",
    REGIME_NORMAL: "Business as usual — standard scanning and conviction thresholds",
    REGIME_CAUTIOUS: "Elevated risk — tighten stops, reduce new entries, favor defensives",
}


# ── Thresholds ───────────────────────────────────────────────────────

# SPX support/resistance levels (updated periodically from analyst research)
SPX_SUPPORT_LOW = 6200       # Newton's lower support target
SPX_SUPPORT_HIGH = 6300      # Newton's upper support target
SPX_RESISTANCE_1 = 6543      # First resistance on bounce
SPX_RESISTANCE_2 = 6584      # Second resistance
SPX_RESISTANCE_CONFIRM = 6653  # Break above = trend confirmed bullish

# QQQ (Nasdaq 100 ETF) levels
QQQ_RESISTANCE_1 = 581       # First resistance
QQQ_RESISTANCE_2 = 586       # Second resistance

# VIX thresholds
VIX_ELEVATED = 25            # Above this = market stressed
VIX_FEAR = 30                # Above this = fear
VIX_CAPITULATION = 35        # Above this = potential capitulation

# SPX drawdown from recent high (%)
DRAWDOWN_MODERATE = 5.0      # 5% from high = moderate correction
DRAWDOWN_DEEP = 8.0          # 8% from high = deep correction
DRAWDOWN_SEVERE = 12.0       # 12% from high = severe — potential opportunity

# Crude oil thresholds (USO ETF price as proxy)
# Note: $110 WTI ≈ USO in $130s range approximately
CRUDE_ELEVATED = 135         # USO price signaling crude stress

# 10Y yield proxy: TLT (20+ Year Treasury ETF) — falling TLT = rising yields
# TLT below ~85 signals yields pushing uncomfortably high
TLT_YIELD_STRESS = 85        # TLT below this = yields elevated, equity headwind

# US Dollar proxy: UUP (Dollar Bull ETF) — rising = stronger dollar
UUP_STRONG = 28.5            # Dollar strength headwind


@dataclass
class MacroSnapshot:
    """Point-in-time macro data."""
    timestamp: str
    spx_price: float = 0.0
    spx_change_pct: float = 0.0
    spx_52w_high: float = 0.0
    spx_drawdown_pct: float = 0.0      # % below recent high
    spx_vs_resistance: str = ""         # Where SPX sits relative to key levels
    qqq_price: float = 0.0
    qqq_change_pct: float = 0.0
    qqq_vs_resistance: str = ""         # Where QQQ sits relative to key levels
    vix_level: float = 0.0
    vix_change_pct: float = 0.0
    crude_price: float = 0.0           # USO ETF as proxy
    crude_change_pct: float = 0.0
    tlt_price: float = 0.0             # Treasury bond ETF (inverse of yields)
    tlt_change_pct: float = 0.0
    uup_price: float = 0.0             # US Dollar ETF
    uup_change_pct: float = 0.0
    regime: str = REGIME_NORMAL
    regime_score: int = 0              # -10 (max fear) to +10 (max greed)
    regime_description: str = ""
    signals: list = field(default_factory=list)   # Human-readable signal list
    previous_regime: str = ""
    regime_changed: bool = False

    # Regime-adjusted parameters
    min_conviction_override: Optional[int] = None
    scan_depth_override: Optional[int] = None
    analysis_depth_override: Optional[int] = None


class MacroMonitor:
    def __init__(self, config: Config):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._state_path = config.DATA_DIR / "macro_state.json"
        self._history_path = config.DATA_DIR / "macro_history.json"
        self._last_snapshot: MacroSnapshot | None = self._load_state()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Data fetching ────────────────────────────────────────────────

    async def _fmp_quote(self, symbol: str) -> dict | None:
        """Fetch a single quote from FMP."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{FMP_BASE}/quote",
                params={"apikey": self.config.FMP_API_KEY, "symbol": symbol},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
        except Exception as e:
            logger.warning("FMP quote failed for %s: %s", symbol, e)
        return None

    async def _fmp_history(self, symbol: str, from_date: str, to_date: str) -> list[dict]:
        """Fetch historical prices from FMP."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{FMP_BASE}/historical-price-eod/light",
                params={
                    "apikey": self.config.FMP_API_KEY,
                    "symbol": symbol,
                    "from": from_date,
                    "to": to_date,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("historical", [])
        except Exception as e:
            logger.warning("FMP history failed for %s: %s", symbol, e)
        return []

    # ── Main check ───────────────────────────────────────────────────

    async def check_conditions(self) -> MacroSnapshot:
        """Fetch all macro data and compute the current regime."""
        now = datetime.now(timezone.utc)
        snapshot = MacroSnapshot(timestamp=now.isoformat())

        # 1. Fetch current quotes
        spx = await self._fmp_quote("^GSPC")
        qqq = await self._fmp_quote("QQQ")
        vix = await self._fmp_quote("^VIX")
        crude = await self._fmp_quote("USO")
        tlt = await self._fmp_quote("TLT")
        uup = await self._fmp_quote("UUP")

        if spx:
            snapshot.spx_price = spx.get("price", 0)
            snapshot.spx_change_pct = spx.get("changesPercentage", 0) or 0
            # Determine where SPX sits relative to key levels
            p = snapshot.spx_price
            if p >= SPX_RESISTANCE_CONFIRM:
                snapshot.spx_vs_resistance = f"Above {SPX_RESISTANCE_CONFIRM} — uptrend confirmed"
            elif p >= SPX_RESISTANCE_2:
                snapshot.spx_vs_resistance = f"Between R2 ({SPX_RESISTANCE_2}) and confirmation ({SPX_RESISTANCE_CONFIRM})"
            elif p >= SPX_RESISTANCE_1:
                snapshot.spx_vs_resistance = f"Between R1 ({SPX_RESISTANCE_1}) and R2 ({SPX_RESISTANCE_2})"
            elif p >= SPX_SUPPORT_HIGH:
                snapshot.spx_vs_resistance = f"Below R1 ({SPX_RESISTANCE_1}), above support ({SPX_SUPPORT_HIGH})"
            elif p >= SPX_SUPPORT_LOW:
                snapshot.spx_vs_resistance = f"IN SUPPORT ZONE ({SPX_SUPPORT_LOW}-{SPX_SUPPORT_HIGH})"
            else:
                snapshot.spx_vs_resistance = f"BELOW SUPPORT ({SPX_SUPPORT_LOW}) — deep value zone"

        if qqq:
            snapshot.qqq_price = qqq.get("price", 0)
            snapshot.qqq_change_pct = qqq.get("changesPercentage", 0) or 0
            p = snapshot.qqq_price
            if p >= QQQ_RESISTANCE_2:
                snapshot.qqq_vs_resistance = f"Above R2 ({QQQ_RESISTANCE_2}) — strength confirmed"
            elif p >= QQQ_RESISTANCE_1:
                snapshot.qqq_vs_resistance = f"Between R1 ({QQQ_RESISTANCE_1}) and R2 ({QQQ_RESISTANCE_2})"
            else:
                snapshot.qqq_vs_resistance = f"Below R1 ({QQQ_RESISTANCE_1})"

        if vix:
            snapshot.vix_level = vix.get("price", 0)
            snapshot.vix_change_pct = vix.get("changesPercentage", 0) or 0

        if crude:
            snapshot.crude_price = crude.get("price", 0)
            snapshot.crude_change_pct = crude.get("changesPercentage", 0) or 0

        if tlt:
            snapshot.tlt_price = tlt.get("price", 0)
            snapshot.tlt_change_pct = tlt.get("changesPercentage", 0) or 0

        if uup:
            snapshot.uup_price = uup.get("price", 0)
            snapshot.uup_change_pct = uup.get("changesPercentage", 0) or 0

        # 2. Calculate SPX drawdown from recent high
        from_date = (now - __import__("datetime").timedelta(days=90)).strftime("%Y-%m-%d")
        to_date = now.strftime("%Y-%m-%d")
        history = await self._fmp_history("^GSPC", from_date, to_date)
        if history:
            prices = [h.get("price", 0) for h in history if h.get("price")]
            if prices:
                recent_high = max(prices)
                snapshot.spx_52w_high = recent_high
                if recent_high > 0 and snapshot.spx_price > 0:
                    snapshot.spx_drawdown_pct = ((recent_high - snapshot.spx_price) / recent_high) * 100

        # 3. Compute regime
        self._compute_regime(snapshot)

        # 4. Check for regime change
        if self._last_snapshot:
            snapshot.previous_regime = self._last_snapshot.regime
            snapshot.regime_changed = snapshot.regime != self._last_snapshot.regime

        # 5. Persist
        self._last_snapshot = snapshot
        self._save_state(snapshot)
        self._append_history(snapshot)

        logger.info(
            "Macro check: SPX=%.0f (%.1f%% from high), VIX=%.1f, Regime=%s (score=%d)",
            snapshot.spx_price, snapshot.spx_drawdown_pct, snapshot.vix_level,
            snapshot.regime, snapshot.regime_score,
        )

        return snapshot

    def _compute_regime(self, snap: MacroSnapshot):
        """Score each signal and determine the overall regime."""
        score = 0  # Negative = fear/opportunity, Positive = complacency/risk
        signals = []

        # ── SPX level signals ────────────────────────────────────────
        if snap.spx_price > 0:
            if snap.spx_price <= SPX_SUPPORT_LOW:
                score -= 3
                signals.append(f"SPX at/below deep support ({SPX_SUPPORT_LOW}) — strong buy zone")
            elif snap.spx_price <= SPX_SUPPORT_HIGH:
                score -= 2
                signals.append(f"SPX in support zone ({SPX_SUPPORT_LOW}-{SPX_SUPPORT_HIGH}) — buy zone")
            elif snap.spx_price >= SPX_RESISTANCE_CONFIRM:
                score += 2
                signals.append(f"SPX above {SPX_RESISTANCE_CONFIRM} — uptrend confirmed")

        # ── SPX drawdown signals ─────────────────────────────────────
        dd = snap.spx_drawdown_pct
        if dd >= DRAWDOWN_SEVERE:
            score -= 3
            signals.append(f"SPX drawdown {dd:.1f}% — severe correction, potential opportunity")
        elif dd >= DRAWDOWN_DEEP:
            score -= 2
            signals.append(f"SPX drawdown {dd:.1f}% — deep correction")
        elif dd >= DRAWDOWN_MODERATE:
            score -= 1
            signals.append(f"SPX drawdown {dd:.1f}% — moderate correction")

        # ── VIX signals ──────────────────────────────────────────────
        vix = snap.vix_level
        if vix >= VIX_CAPITULATION:
            score -= 3
            signals.append(f"VIX at {vix:.1f} — capitulation level, contrarian buy signal")
        elif vix >= VIX_FEAR:
            score -= 2
            signals.append(f"VIX at {vix:.1f} — fear elevated, approaching buy zone")
        elif vix >= VIX_ELEVATED:
            score -= 1
            signals.append(f"VIX at {vix:.1f} — elevated stress")
        elif vix > 0 and vix < 15:
            score += 1
            signals.append(f"VIX at {vix:.1f} — low volatility, complacency risk")

        # ── Crude oil signals ────────────────────────────────────────
        if snap.crude_price > CRUDE_ELEVATED:
            score += 1
            signals.append(f"Crude (USO) at ${snap.crude_price:.0f} — oil elevated, headwind for equities")

        # ── 10Y yield signals (TLT as inverse proxy) ────────────────
        if snap.tlt_price > 0 and snap.tlt_price < TLT_YIELD_STRESS:
            score += 1
            signals.append(f"TLT at ${snap.tlt_price:.2f} (below {TLT_YIELD_STRESS}) — yields elevated, equity headwind")
        elif snap.tlt_price > 0 and snap.tlt_change_pct > 1.0:
            signals.append(f"TLT up {snap.tlt_change_pct:+.1f}% — yields falling, positive for equities")

        # ── US Dollar signals ─────────────────────────────────────────
        if snap.uup_price > UUP_STRONG:
            score += 1
            signals.append(f"Dollar (UUP) at ${snap.uup_price:.2f} — strong dollar, headwind for equities")

        # ── SPX/QQQ level awareness ───────────────────────────────────
        if snap.spx_vs_resistance:
            signals.append(f"SPX positioning: {snap.spx_vs_resistance}")
        if snap.qqq_vs_resistance:
            signals.append(f"QQQ positioning: {snap.qqq_vs_resistance}")

        # ── Determine regime ─────────────────────────────────────────
        snap.regime_score = max(-10, min(10, score))
        snap.signals = signals

        if score <= -5:
            snap.regime = REGIME_AGGRESSIVE_DEPLOY
            snap.min_conviction_override = 6      # Lower bar — more stocks qualify
            snap.scan_depth_override = 80          # Scan more of the universe
            snap.analysis_depth_override = 30      # Deep-dive on more candidates
        elif score <= -3:
            snap.regime = REGIME_DIP_OPPORTUNITY
            snap.min_conviction_override = 6
            snap.scan_depth_override = 70
            snap.analysis_depth_override = 25
        elif score >= 3:
            snap.regime = REGIME_CAUTIOUS
            snap.min_conviction_override = 8      # Higher bar — only strongest picks
            snap.scan_depth_override = 40
            snap.analysis_depth_override = 15
        else:
            snap.regime = REGIME_NORMAL
            snap.min_conviction_override = None    # Use config defaults
            snap.scan_depth_override = None
            snap.analysis_depth_override = None

        snap.regime_description = REGIME_DESCRIPTIONS.get(snap.regime, "")

    # ── Convenience getters ──────────────────────────────────────────

    def get_current_regime(self) -> str:
        """Return current regime string."""
        if self._last_snapshot:
            return self._last_snapshot.regime
        return REGIME_NORMAL

    def get_last_snapshot(self) -> MacroSnapshot | None:
        return self._last_snapshot

    def get_effective_min_conviction(self) -> int:
        """Return the effective minimum conviction based on current regime."""
        if self._last_snapshot and self._last_snapshot.min_conviction_override is not None:
            return self._last_snapshot.min_conviction_override
        return self.config.MIN_CONVICTION

    def get_effective_universe_size(self) -> int:
        """Return the effective universe scan size based on current regime."""
        if self._last_snapshot and self._last_snapshot.scan_depth_override is not None:
            return self._last_snapshot.scan_depth_override
        return self.config.UNIVERSE_SIZE

    def get_effective_analysis_depth(self) -> int:
        """Return the effective deep-analysis count based on current regime."""
        if self._last_snapshot and self._last_snapshot.analysis_depth_override is not None:
            return self._last_snapshot.analysis_depth_override
        return self.config.DEEP_ANALYSIS_SIZE

    # ── Persistence ──────────────────────────────────────────────────

    def _save_state(self, snapshot: MacroSnapshot):
        """Save latest snapshot to disk."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(asdict(snapshot), indent=2))
        except Exception as e:
            logger.error("Failed to save macro state: %s", e)

    def _load_state(self) -> MacroSnapshot | None:
        """Load last snapshot from disk."""
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text())
                return MacroSnapshot(**data)
        except Exception as e:
            logger.warning("Failed to load macro state: %s", e)
        return None

    def _append_history(self, snapshot: MacroSnapshot):
        """Append snapshot to history file (keep last 90 entries)."""
        try:
            history = []
            if self._history_path.exists():
                history = json.loads(self._history_path.read_text())
            history.append(asdict(snapshot))
            # Keep last 90 entries
            history = history[-90:]
            self._history_path.write_text(json.dumps(history, indent=2))
        except Exception as e:
            logger.error("Failed to append macro history: %s", e)

    # ── Discord format helper ────────────────────────────────────────

    def format_discord_brief(self, snapshot: MacroSnapshot) -> dict:
        """Build a Discord embed dict for the macro brief."""
        regime_colors = {
            REGIME_AGGRESSIVE_DEPLOY: 0x22C55E,  # Green — go time
            REGIME_DIP_OPPORTUNITY: 0xF59E0B,     # Amber — getting ready
            REGIME_NORMAL: 0x3B82F6,              # Blue — standard
            REGIME_CAUTIOUS: 0xEF4444,            # Red — careful
        }

        regime_emojis = {
            REGIME_AGGRESSIVE_DEPLOY: "\U0001f7e2",
            REGIME_DIP_OPPORTUNITY: "\U0001f7e1",
            REGIME_NORMAL: "\U0001f535",
            REGIME_CAUTIOUS: "\U0001f534",
        }

        color = regime_colors.get(snapshot.regime, 0x6B7280)
        emoji = regime_emojis.get(snapshot.regime, "\u26aa")

        # Regime bar visualization
        score = snapshot.regime_score
        bar_len = 21  # -10 to +10
        bar_pos = max(0, min(20, score + 10))
        bar = "\u2591" * bar_pos + "\u2588" + "\u2591" * (20 - bar_pos)
        bar_label = f"Fear [{bar}] Greed"

        fields = [
            {"name": "Regime", "value": f"{emoji} **{snapshot.regime.replace('_', ' ').title()}**", "inline": True},
            {"name": "Score", "value": f"`{score:+d}` / \u00b110\n{bar_label}", "inline": True},
            {"name": "\u200b", "value": "\u200b", "inline": True},
            {"name": "S&P 500", "value": f"${snapshot.spx_price:,.0f} ({snapshot.spx_change_pct:+.1f}%)\n{snapshot.spx_vs_resistance}", "inline": True},
            {"name": "QQQ", "value": f"${snapshot.qqq_price:,.2f} ({snapshot.qqq_change_pct:+.1f}%)\n{snapshot.qqq_vs_resistance}", "inline": True},
            {"name": "Drawdown", "value": f"{snapshot.spx_drawdown_pct:.1f}% from 90d high", "inline": True},
            {"name": "VIX", "value": f"{snapshot.vix_level:.1f} ({snapshot.vix_change_pct:+.1f}%)", "inline": True},
            {"name": "10Y Yield (TLT)", "value": f"${snapshot.tlt_price:.2f} ({snapshot.tlt_change_pct:+.1f}%)\n{'\u26a0 Yields elevated' if snapshot.tlt_price < TLT_YIELD_STRESS else 'Yields normal'}", "inline": True},
            {"name": "Crude (USO)", "value": f"${snapshot.crude_price:.2f} ({snapshot.crude_change_pct:+.1f}%)", "inline": True},
            {"name": "Dollar (UUP)", "value": f"${snapshot.uup_price:.2f} ({snapshot.uup_change_pct:+.1f}%)", "inline": True},
            {"name": "\u200b", "value": "\u200b", "inline": True},
            {"name": "\u200b", "value": "\u200b", "inline": True},
        ]

        if snapshot.signals:
            signal_text = "\n".join(f"\u2022 {s}" for s in snapshot.signals)
            fields.append({"name": "\U0001f4e1 Signals", "value": signal_text[:1024], "inline": False})

        # Regime-specific action
        if snapshot.regime == REGIME_AGGRESSIVE_DEPLOY:
            action = (
                "**Action:** Widening scan to 80 stocks, analyzing top 30, "
                "lowering conviction threshold to 6/10. Actively filling positions."
            )
        elif snapshot.regime == REGIME_DIP_OPPORTUNITY:
            action = (
                "**Action:** Widening scan to 70 stocks, analyzing top 25, "
                "lowering conviction threshold to 6/10. Preparing to deploy."
            )
        elif snapshot.regime == REGIME_CAUTIOUS:
            action = (
                "**Action:** Raising conviction threshold to 8/10, "
                "tightening scan. Only highest-conviction new entries."
            )
        else:
            action = "**Action:** Standard scanning parameters. Business as usual."

        fields.append({"name": "\U0001f3af Bot Response", "value": action, "inline": False})

        if snapshot.regime_changed:
            fields.append({
                "name": "\u26a1 Regime Change",
                "value": f"Changed from **{snapshot.previous_regime.replace('_', ' ').title()}** → **{snapshot.regime.replace('_', ' ').title()}**",
                "inline": False,
            })

        embed = {
            "title": f"\U0001f30d Macro Brief — {snapshot.regime.replace('_', ' ').title()}",
            "description": snapshot.regime_description,
            "color": color,
            "fields": fields,
        }

        return embed
