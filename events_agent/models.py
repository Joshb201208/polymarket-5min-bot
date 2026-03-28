"""Data models for the Events betting agent."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional


class EventCategory(str, Enum):
    POLITICS = "politics"
    GEOPOLITICS = "geopolitics"
    ECONOMICS = "economics"
    CRYPTO = "crypto"
    CULTURE = "culture"
    SCIENCE = "science"
    ENTERTAINMENT = "entertainment"
    TECHNOLOGY = "technology"
    OTHER = "other"


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    WON = "won"
    LOST = "lost"


@dataclass
class EventMarket:
    """A Polymarket events market (non-sports)."""
    id: str
    question: str
    slug: str
    end_date: str
    outcomes: list[str]
    outcome_prices: list[float]
    clob_token_ids: list[str]
    liquidity: float
    volume_24h: float
    active: bool
    closed: bool
    accepting_orders: bool
    neg_risk: bool
    category: EventCategory = EventCategory.OTHER
    event_slug: str = ""
    event_title: str = ""
    description: str = ""

    @classmethod
    def from_api(cls, raw: dict, event_slug: str = "", event_title: str = "") -> EventMarket:
        outcomes = json.loads(raw.get("outcomes", "[]"))
        prices = json.loads(raw.get("outcomePrices", "[]"))
        token_ids = json.loads(raw.get("clobTokenIds", "[]"))
        return cls(
            id=str(raw.get("id", "")),
            question=raw.get("question", ""),
            slug=raw.get("slug", ""),
            end_date=raw.get("endDate", ""),
            outcomes=outcomes,
            outcome_prices=[float(p) for p in prices],
            clob_token_ids=token_ids,
            liquidity=float(raw.get("liquidityNum", 0)),
            volume_24h=float(raw.get("volume24hr", 0)),
            active=bool(raw.get("active", False)),
            closed=bool(raw.get("closed", False)),
            accepting_orders=bool(raw.get("acceptingOrders", False)),
            neg_risk=bool(raw.get("negRisk", False)),
            event_slug=event_slug,
            event_title=event_title,
            description=raw.get("description", ""),
        )


@dataclass
class EdgeResult:
    """Result of an edge calculation for an events market."""
    market: EventMarket
    our_fair_price: float
    market_price: float
    edge: float
    confidence: Confidence
    side: str  # "YES" or "NO"
    side_index: int  # index into outcomes
    edge_source: str = ""  # e.g., "spread_analysis", "time_decay", "liquidity_imbalance"

    @property
    def has_edge(self) -> bool:
        return self.edge >= 0.05  # 5% minimum


@dataclass
class Position:
    """A tracked events position (open or closed)."""
    id: str
    market_id: str
    market_question: str
    token_id: str
    side: str
    entry_price: float
    shares: float
    cost: float
    entry_time: str
    confidence: str
    edge_at_entry: float
    our_fair_price: float
    mode: str
    agent: str = "events"
    status: str = "open"
    category: str = "other"
    market_end_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl: Optional[float] = None
    exit_reason: Optional[str] = None
    market_slug: str = ""
    fees_paid: float = 0.0
    edge_source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Position:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Trade:
    """A completed trade for logging."""
    id: str
    position_id: str
    market_id: str
    market_question: str
    action: str  # "BUY" or "SELL"
    side: str
    price: float
    shares: float
    amount: float
    timestamp: str
    mode: str
    agent: str = "events"
    order_id: str = ""
    pnl: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Trade:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
