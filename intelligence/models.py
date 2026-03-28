"""Shared data models for intelligence signals."""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional


@dataclass
class Signal:
    """A single intelligence signal from any source module."""

    source: str  # "x_scanner", "orderbook", "metaculus", etc.
    market_id: str  # Polymarket condition_id or slug
    market_question: str  # Human-readable question
    signal_type: str  # "sentiment", "whale", "divergence", "momentum", etc.
    direction: str  # "YES", "NO", or "NEUTRAL"
    strength: float  # 0.0 to 1.0
    confidence: float  # 0.0 to 1.0
    details: dict  # Source-specific metadata
    timestamp: datetime
    expires_at: datetime  # When this signal becomes stale

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Signal:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class CompositeScore:
    """Combined score from all intelligence sources for a single market."""

    market_id: str
    composite: float  # 0.0 to 1.0
    direction: str  # "YES" or "NO"
    confidence_tier: str  # "VERY_HIGH", "HIGH", "MEDIUM", "LOW"
    max_bet_pct: float  # Max % of bankroll to bet
    signal_breakdown: dict  # {source: {score, direction, details}}
    consensus_count: int  # How many sources agree on direction
    timestamp: datetime

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CompositeScore:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class CorrelationReport:
    """Report on portfolio correlation and concentration risk."""

    theme_exposure: dict  # {theme: {positions: [], total_usd: float, pct: float}}
    concentration_warnings: list  # Themes over 30%
    diversification_score: int  # 0-100 (100 = perfectly diversified)
    suggested_actions: list  # "Reduce US politics exposure by $X"
    pairwise_correlations: list  # [(market_a, market_b, correlation)]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CorrelationReport:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class IntelligenceReport:
    """Full report from a single intelligence scan cycle."""

    signals: list  # list[Signal]
    scores: dict  # {market_id: CompositeScore}
    correlation: Optional[CorrelationReport]
    timestamp: datetime

    def to_dict(self) -> dict:
        return {
            "signals": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.signals],
            "scores": {
                k: v.to_dict() if hasattr(v, "to_dict") else v
                for k, v in self.scores.items()
            },
            "correlation": self.correlation.to_dict() if self.correlation else None,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }
