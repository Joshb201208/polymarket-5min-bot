"""Macro regime from computed inputs (DECISIONS D5, F-06-05).

Inputs are free broker ETF daily bars from ``PriceService`` (SPY, QQQ, TLT, USO,
UUP and VIXY as the volatility proxy), with an optional FRED VIXCLS reading
behind health reporting. Nothing is hard-coded: levels are rolling 50/200-day
averages and the drawdown from the 90-day high.

Five factors, each an integer in [-2, +2] (negative = risk-off):

    trend        SPY and QQQ close vs their 50d and 200d averages
    drawdown     SPY distance below its 90-day high
    volatility   VIXY vs its 50d average (FRED VIXCLS if VIXY is missing)
    rates        TLT 20-session return (falling bonds = rising yields)
    oil_dollar   USO and UUP 20-session returns (oil shock / dollar squeeze)

A missing or stale input stays ``None`` and never counts as 0. At least four of
the five factors must be fresh; otherwise the prior regime is held and the
snapshot is marked degraded. A regime change never trades: it only sets the
entry thresholds (minimum conviction 7 normally, 8 when cautious, never below 7)
and raises an alert. The regime is persisted in the state store.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from stock_agent.config import Config

logger = logging.getLogger(__name__)

REGIME_AGGRESSIVE_DEPLOY = "AGGRESSIVE_DEPLOY"
REGIME_DIP_OPPORTUNITY = "DIP_OPPORTUNITY"
REGIME_NORMAL = "NORMAL"
REGIME_CAUTIOUS = "CAUTIOUS"

REGIME_DESCRIPTIONS = {
    REGIME_AGGRESSIVE_DEPLOY: "Deep pullback with elevated fear — widen the search for entries",
    REGIME_DIP_OPPORTUNITY: "Moderate pullback — widen scanning, prepare to deploy",
    REGIME_NORMAL: "Business as usual — standard scanning and conviction thresholds",
    REGIME_CAUTIOUS: "Broad risk-off signals — only the strongest new entries",
}

SYMBOLS = ("SPY", "QQQ", "TLT", "USO", "UUP", "VIXY")
FACTORS = ("trend", "drawdown", "volatility", "rates", "oil_dollar")
MIN_FRESH_FACTORS = 4
STALE_AFTER_DAYS = 4          # calendar days since the last completed session (weekend + holiday)
HISTORY_SESSIONS = 210        # enough for a 200-session average
STORE_KEY = "macro.regime"
HISTORY_KEY = "macro.history"

# Factor cut-offs (percent points). Relative measures only; no index levels.
DD_MILD, DD_DIP, DD_DEEP = 3.0, 5.0, 10.0
CAUTIOUS_SCORE = -4


@dataclass
class MacroSnapshot:
    """Point-in-time macro inputs, factors and the resulting regime."""
    timestamp: str
    closes: dict = field(default_factory=dict)          # symbol -> last close (float) or None
    key_levels: dict = field(default_factory=dict)      # symbol -> {close, sma50, sma200, high90}
    returns_20d: dict = field(default_factory=dict)     # symbol -> % or None
    spy_drawdown_pct: Optional[float] = None
    vix_level: Optional[float] = None                   # FRED VIXCLS when available
    factors: dict = field(default_factory=dict)         # factor -> int or None
    fresh_factors: int = 0
    degraded: bool = False
    degraded_reason: str = ""
    regime: str = REGIME_NORMAL
    regime_score: int = 0
    regime_description: str = ""
    signals: list = field(default_factory=list)
    previous_regime: str = ""
    regime_changed: bool = False
    min_conviction_override: Optional[int] = None
    scan_depth_override: Optional[int] = None
    analysis_depth_override: Optional[int] = None

    # Display-only helpers for legacy report code (never used in regime maths).
    @property
    def spx_price(self) -> float:
        return float(self.closes.get("SPY") or 0.0)

    @property
    def qqq_price(self) -> float:
        return float(self.closes.get("QQQ") or 0.0)

    def to_record(self) -> dict:
        return asdict(self)

    @classmethod
    def from_record(cls, rec: dict | None) -> "MacroSnapshot | None":
        if not isinstance(rec, dict) or "timestamp" not in rec:
            return None
        known = {f for f in cls.__dataclass_fields__}
        try:
            return cls(**{k: v for k, v in rec.items() if k in known})
        except TypeError:
            return None


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _ret(closes: list[float], n: int) -> float | None:
    if len(closes) <= n or closes[-n - 1] <= 0:
        return None
    return (closes[-1] / closes[-n - 1] - 1) * 100


class MacroMonitor:
    def __init__(self, config: Any = Config, *, prices: Any = None, store: Any = None,
                 alert: Callable[..., Any] | None = None, report: Callable[..., Any] | None = None,
                 fred: Callable[[str], Awaitable[float | None]] | None = None,
                 now: Callable[[], datetime] | None = None):
        self.config = config
        self.prices = prices
        self.store = store
        self._alert = alert
        self._report = report
        self._fred = fred
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._last_snapshot: MacroSnapshot | None = self._load_state()

    async def close(self) -> None:  # kept for the scheduler's shutdown loop
        return None

    # ------------------------------------------------------------------ inputs
    async def _bars(self) -> dict[str, list]:
        if self.prices is None:
            return {}
        try:
            return await self.prices.get_daily_bars(list(SYMBOLS), sessions=HISTORY_SESSIONS)
        except Exception as e:
            logger.warning("macro: daily bars unavailable: %s", e)
            await self._health("prices", False, str(e))
            return {}

    async def _health(self, provider: str, ok: bool, error: str | None = None) -> None:
        if self._report is None:
            return
        try:
            r = self._report(provider, "macro", ok, error)
            if hasattr(r, "__await__"):
                await r
        except Exception:
            logger.exception("macro health report failed")

    async def _vixcls(self) -> float | None:
        if self._fred is None:
            return None
        try:
            v = _f(await self._fred("VIXCLS"))
            await self._health("fred", v is not None, None if v is not None else "no VIXCLS value")
            return v
        except Exception as e:
            await self._health("fred", False, str(e))
            return None

    def _fresh_closes(self, bars: list, today: date) -> list[float] | None:
        if not bars:
            return None
        last = getattr(bars[-1], "session_date", None)
        if not isinstance(last, date) or (today - last).days > STALE_AFTER_DAYS:
            return None
        out = [_f(getattr(b, "close", None)) for b in bars]
        if any(c is None or c <= 0 for c in out):
            return None
        return out  # type: ignore[return-value]

    # ------------------------------------------------------------------ factors
    @staticmethod
    def _trend(series: dict[str, list[float] | None]) -> int | None:
        scores = []
        for sym in ("SPY", "QQQ"):
            c = series.get(sym)
            if not c:
                continue
            s50, s200 = _sma(c, 50), _sma(c, 200)
            if s50 is None or s200 is None:
                continue
            scores.append((1 if c[-1] > s200 else -1) + (1 if c[-1] > s50 else -1))
        if "SPY" not in {s for s in ("SPY", "QQQ") if series.get(s)} or not scores:
            return None
        return round(sum(scores) / len(scores))

    @staticmethod
    def _drawdown(c: list[float] | None) -> tuple[int | None, float | None]:
        if not c or len(c) < 90:
            return None, None
        dd = (1 - c[-1] / max(c[-90:])) * 100
        score = 1 if dd < DD_MILD else 0 if dd < DD_DIP else -1 if dd < DD_DEEP else -2
        return score, dd

    @staticmethod
    def _volatility(vixy: list[float] | None, vixcls: float | None) -> int | None:
        if vixy:
            s50 = _sma(vixy, 50)
            if s50:
                r = vixy[-1] / s50
                return -2 if r >= 1.25 else -1 if r >= 1.10 else 1 if r <= 0.90 else 0
        if vixcls is not None:
            return -2 if vixcls >= 30 else -1 if vixcls >= 25 else 1 if vixcls <= 15 else 0
        return None

    @staticmethod
    def _rates(tlt: list[float] | None) -> int | None:
        r = _ret(tlt or [], 20)
        if r is None:
            return None
        return -2 if r <= -4 else -1 if r <= -2 else 1 if r >= 2 else 0

    @staticmethod
    def _oil_dollar(uso: list[float] | None, uup: list[float] | None) -> int | None:
        ro, rd = _ret(uso or [], 20), _ret(uup or [], 20)
        if ro is None and rd is None:
            return None
        score = 0
        if ro is not None and ro >= 15:
            score -= 1
        if rd is not None and rd >= 3:
            score -= 1
        return score

    # ------------------------------------------------------------------ check
    async def check_conditions(self) -> MacroSnapshot:
        now = self._now()
        today = now.date()
        raw = await self._bars()
        series = {s: self._fresh_closes(raw.get(s) or [], today) for s in SYMBOLS}
        vixcls = await self._vixcls()
        snap = MacroSnapshot(timestamp=now.isoformat())
        for s in SYMBOLS:
            c = series[s]
            snap.closes[s] = c[-1] if c else None
            snap.returns_20d[s] = _ret(c or [], 20)
            snap.key_levels[s] = {"close": c[-1] if c else None, "sma50": _sma(c or [], 50),
                                  "sma200": _sma(c or [], 200),
                                  "high90": max(c[-90:]) if c and len(c) >= 90 else None}
        snap.vix_level = vixcls
        dd_score, dd = self._drawdown(series["SPY"])
        snap.spy_drawdown_pct = dd
        snap.factors = {
            "trend": self._trend(series),
            "drawdown": dd_score,
            "volatility": self._volatility(series["VIXY"], vixcls),
            "rates": self._rates(series["TLT"]),
            "oil_dollar": self._oil_dollar(series["USO"], series["UUP"]),
        }
        snap.fresh_factors = sum(v is not None for v in snap.factors.values())
        await self._health("prices", any(series.values()),
                           None if any(series.values()) else "no fresh macro bars")
        prev = self._last_snapshot
        snap.previous_regime = prev.regime if prev else ""
        self._compute_regime(snap, prev)
        snap.regime_changed = bool(prev) and snap.regime != prev.regime
        self._last_snapshot = snap
        self._save_state(snap)
        if snap.degraded:
            await self._send("warning", "Macro inputs degraded",
                             f"{snap.degraded_reason}. Holding the {snap.regime.replace('_', ' ').lower()} regime.",
                             key="macro-degraded")
        if snap.regime_changed:
            await self._send("info", "Macro regime changed",
                             f"{snap.previous_regime} -> {snap.regime}: minimum conviction "
                             f"{self.get_effective_min_conviction()}/10. No trades are placed by this change.",
                             key=f"macro-regime-{snap.regime}")
        return snap

    def _compute_regime(self, snap: MacroSnapshot, prev: MacroSnapshot | None) -> None:
        f = snap.factors
        signals = [f"{k}: {'n/a' if v is None else f'{v:+d}'}" for k, v in f.items()]
        if snap.spy_drawdown_pct is not None:
            signals.append(f"SPY {snap.spy_drawdown_pct:.1f}% below its 90-day high")
        snap.signals = signals
        score = sum(v for v in f.values() if v is not None)
        snap.regime_score = max(-10, min(10, score))
        if snap.fresh_factors < MIN_FRESH_FACTORS:
            missing = [k for k, v in f.items() if v is None]
            snap.degraded = True
            snap.degraded_reason = (f"only {snap.fresh_factors}/5 macro factors fresh "
                                    f"(missing: {', '.join(missing)})")
            snap.regime = prev.regime if prev else REGIME_NORMAL
        else:
            dd = snap.spy_drawdown_pct
            vol = f.get("volatility")
            if dd is not None and dd >= DD_DEEP and vol is not None and vol <= -1:
                snap.regime = REGIME_AGGRESSIVE_DEPLOY
            elif score <= CAUTIOUS_SCORE:
                snap.regime = REGIME_CAUTIOUS
            elif dd is not None and dd >= DD_DIP:
                snap.regime = REGIME_DIP_OPPORTUNITY
            else:
                snap.regime = REGIME_NORMAL
        base = max(7, int(getattr(self.config, "MIN_CONVICTION", 7) or 7))
        uni = int(getattr(self.config, "UNIVERSE_SIZE", 50) or 50)
        deep = int(getattr(self.config, "DEEP_ANALYSIS_SIZE", 20) or 20)
        if snap.regime == REGIME_CAUTIOUS:
            snap.min_conviction_override = max(8, base)
            snap.scan_depth_override, snap.analysis_depth_override = max(1, uni * 4 // 5), max(1, deep * 3 // 4)
        elif snap.regime in (REGIME_AGGRESSIVE_DEPLOY, REGIME_DIP_OPPORTUNITY):
            wide = 8 if snap.regime == REGIME_AGGRESSIVE_DEPLOY else 7
            snap.min_conviction_override = base
            snap.scan_depth_override = uni * wide // 5
            snap.analysis_depth_override = deep * wide // 5
        else:
            snap.min_conviction_override = base
            snap.scan_depth_override = snap.analysis_depth_override = None
        snap.regime_description = REGIME_DESCRIPTIONS.get(snap.regime, "")

    async def _send(self, sev: str, title: str, body: str, key: str | None = None) -> None:
        if self._alert is None:
            return
        try:
            r = self._alert(sev, title, body, key=key)
            if hasattr(r, "__await__"):
                await r
        except Exception:
            logger.exception("macro alert failed")

    # ------------------------------------------------------------------ getters
    def get_current_regime(self) -> str:
        return self._last_snapshot.regime if self._last_snapshot else REGIME_NORMAL

    def get_last_snapshot(self) -> MacroSnapshot | None:
        return self._last_snapshot

    def get_effective_min_conviction(self) -> int:
        base = max(7, int(getattr(self.config, "MIN_CONVICTION", 7) or 7))
        s = self._last_snapshot
        if s and s.min_conviction_override is not None:
            return max(7, int(s.min_conviction_override))
        return base

    def get_effective_universe_size(self) -> int:
        s = self._last_snapshot
        if s and s.scan_depth_override is not None:
            return s.scan_depth_override
        return self.config.UNIVERSE_SIZE

    def get_effective_analysis_depth(self) -> int:
        s = self._last_snapshot
        if s and s.analysis_depth_override is not None:
            return s.analysis_depth_override
        return self.config.DEEP_ANALYSIS_SIZE

    # ------------------------------------------------------------------ state
    def _save_state(self, snap: MacroSnapshot) -> None:
        if self.store is None:
            return
        try:
            self.store.kv_set(STORE_KEY, snap.to_record())
            hist = list(self.store.kv_get(HISTORY_KEY, []) or [])
            hist.append({"timestamp": snap.timestamp, "regime": snap.regime, "score": snap.regime_score,
                         "fresh": snap.fresh_factors, "degraded": snap.degraded})
            self.store.kv_set(HISTORY_KEY, hist[-90:])
        except Exception as e:
            logger.error("Failed to save macro state: %s", e)

    def _load_state(self) -> MacroSnapshot | None:
        if self.store is None:
            return None
        try:
            return MacroSnapshot.from_record(self.store.kv_get(STORE_KEY, None))
        except Exception as e:
            logger.warning("Failed to load macro state: %s", e)
            return None

    # ------------------------------------------------------------------ report
    def format_discord_brief(self, snapshot: MacroSnapshot) -> dict:
        colors = {REGIME_AGGRESSIVE_DEPLOY: 0x22C55E, REGIME_DIP_OPPORTUNITY: 0xF59E0B,
                  REGIME_NORMAL: 0x3B82F6, REGIME_CAUTIOUS: 0xEF4444}

        def money(v: Any) -> str:
            return "n/a" if v is None else f"${v:,.2f}"

        def pct(v: Any) -> str:
            return "n/a" if v is None else f"{v:+.1f}%"

        fields = [
            {"name": "Regime", "value": f"**{snapshot.regime.replace('_', ' ').title()}**", "inline": True},
            {"name": "Score", "value": f"`{snapshot.regime_score:+d}` ({snapshot.fresh_factors}/5 factors fresh)",
             "inline": True},
            {"name": "Min conviction", "value": f"{snapshot.min_conviction_override or 7}/10", "inline": True},
        ]
        for sym, label in (("SPY", "S&P 500 (SPY)"), ("QQQ", "Nasdaq 100 (QQQ)"), ("TLT", "Long bonds (TLT)"),
                           ("USO", "Oil (USO)"), ("UUP", "Dollar (UUP)"), ("VIXY", "Volatility (VIXY)")):
            lv = snapshot.key_levels.get(sym) or {}
            fields.append({"name": label, "value": f"{money(lv.get('close'))} · 20d {pct(snapshot.returns_20d.get(sym))}"
                           f"\n50d {money(lv.get('sma50'))} · 200d {money(lv.get('sma200'))}", "inline": True})
        if snapshot.spy_drawdown_pct is not None:
            fields.append({"name": "Drawdown", "value": f"{snapshot.spy_drawdown_pct:.1f}% below the 90-day high",
                           "inline": True})
        if snapshot.signals:
            fields.append({"name": "Factors", "value": "\n".join(f"• {s}" for s in snapshot.signals)[:1024],
                           "inline": False})
        if snapshot.degraded:
            fields.append({"name": "Degraded", "value": snapshot.degraded_reason[:1024], "inline": False})
        if snapshot.regime_changed:
            fields.append({"name": "Regime change", "value": f"{snapshot.previous_regime} → {snapshot.regime}. "
                           "This only changes thresholds; it places no trades.", "inline": False})
        return {"title": f"Macro Brief — {snapshot.regime.replace('_', ' ').title()}",
                "description": snapshot.regime_description, "color": colors.get(snapshot.regime, 0x6B7280),
                "fields": fields}


__all__ = [
    "FACTORS", "MacroMonitor", "MacroSnapshot", "REGIME_AGGRESSIVE_DEPLOY", "REGIME_CAUTIOUS",
    "REGIME_DESCRIPTIONS", "REGIME_DIP_OPPORTUNITY", "REGIME_NORMAL",
]
