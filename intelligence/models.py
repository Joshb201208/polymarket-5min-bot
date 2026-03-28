"""Shared data models for all intelligence modules."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


@dataclass
class Signal:
    """A signal produced by any intelligence module."""
    source: str           # "x_scanner", "orderbook", "metaculus", etc.
    market_id: str        # Polymarket condition_id or slug
    market_question: str  # Human-readable question
    signal_type: str      # "sentiment", "whale", "divergence", "momentum", etc.
    direction: str        # "YES", "NO", or "NEUTRAL"
    strength: float       # 0.0 to 1.0
    confidence: float     # 0.0 to 1.0
    details: dict = field(default_factory=dict)
    timestamp: str = ""
    expires_at: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if not self.expires_at:
            # Default: signal expires in 1 hour
            from datetime import timedelta
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            self.expires_at = expires.isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Signal:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def is_expired(self) -> bool:
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) > exp
        except (ValueError, AttributeError):
            return True


@dataclass
class CompositeScore:
    """Combined confidence score for a market from all signal sources."""
    market_id: str
    composite: float           # 0.0 to 1.0
    direction: str             # "YES" or "NO"
    confidence_tier: str       # "VERY_HIGH", "HIGH", "MEDIUM", "LOW"
    max_bet_pct: float         # Max % of bankroll to bet
    signal_breakdown: dict = field(default_factory=dict)
    consensus_count: int = 0
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CompositeScore:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class CorrelationReport:
    """Report on position correlation and concentration risk."""
    theme_exposure: dict = field(default_factory=dict)
    concentration_warnings: list = field(default_factory=list)
    diversification_score: int = 100
    suggested_actions: list = field(default_factory=list)
    pairwise_correlations: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CorrelationReport:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class IntelligenceReport:
    """Full intelligence report from one scan cycle."""
    signals: list = field(default_factory=list)          # list[Signal]
    scores: dict = field(default_factory=dict)            # {market_id: CompositeScore}
    correlation: CorrelationReport = field(default_factory=CorrelationReport)
    timestamp: str = ""
    source_health: dict = field(default_factory=dict)     # {source: {status, last_update, error}}

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        d = {
            "signals": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.signals],
            "scores": {
                k: v.to_dict() if hasattr(v, "to_dict") else v
                for k, v in self.scores.items()
            },
            "correlation": self.correlation.to_dict() if hasattr(self.correlation, "to_dict") else self.correlation,
            "timestamp": self.timestamp,
            "source_health": self.source_health,
        }
        return d


@dataclass
class BacktestReport:
    """Results from a signal backtest run."""
    period_days: int = 30
    total_signals: int = 0
    by_source: dict = field(default_factory=dict)   # {source: {signals, win_rate, avg_pnl, sharpe}}
    by_tier: dict = field(default_factory=dict)      # {tier: {signals, win_rate, avg_pnl}}
    equity_curve: list = field(default_factory=list)  # [(date, cumulative_pnl)]
    best_source: str = ""
    worst_source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> BacktestReport:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
