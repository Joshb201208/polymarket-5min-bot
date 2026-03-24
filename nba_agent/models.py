"""Data models for the NBA betting agent."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional


class MarketType(str, Enum):
    MONEYLINE = "moneyline"
    SPREAD = "spread"
    TOTAL = "total"
    CHAMPIONSHIP = "championship"
    MVP = "mvp"
    CONFERENCE = "conference"
    UNKNOWN = "unknown"


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
class Market:
    """A Polymarket NBA market."""
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
    game_start_time: Optional[str] = None
    market_type: MarketType = MarketType.UNKNOWN
    event_slug: str = ""
    event_title: str = ""

    @classmethod
    def from_api(cls, raw: dict, event_slug: str = "", event_title: str = "") -> Market:
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
            game_start_time=raw.get("gameStartTime"),
            event_slug=event_slug,
            event_title=event_title,
        )

    def detect_market_type(self) -> MarketType:
        slug = self.slug.lower()
        if "-spread-" in slug:
            return MarketType.SPREAD
        if "-total-" in slug:
            return MarketType.TOTAL
        if "nba-finals" in slug or "nba-champion" in slug:
            return MarketType.CHAMPIONSHIP
        if "nba-mvp" in slug:
            return MarketType.MVP
        if "conference-finals" in slug or "conference-champion" in slug:
            return MarketType.CONFERENCE
        # Moneyline: nba-{away}-{home}-{date} with no extra suffix
        if slug.startswith("nba-") and "-spread-" not in slug and "-total-" not in slug:
            parts = slug.split("-")
            # Moneyline slugs: nba-{team1}-{team2}-{date} e.g. nba-lac-dal-2026-03-21
            if len(parts) >= 5:
                # Check if the last 3 parts are a date
                try:
                    int(parts[-3])
                    int(parts[-2])
                    int(parts[-1])
                    return MarketType.MONEYLINE
                except (ValueError, IndexError):
                    pass
        return MarketType.UNKNOWN

    @property
    def is_game_market(self) -> bool:
        return self.market_type in (MarketType.MONEYLINE, MarketType.SPREAD, MarketType.TOTAL)

    @property
    def is_futures_market(self) -> bool:
        return self.market_type in (MarketType.CHAMPIONSHIP, MarketType.MVP, MarketType.CONFERENCE)

    @property
    def min_edge(self) -> float:
        if self.is_futures_market:
            return 0.07  # 7% for futures
        return 0.04  # 4% for game markets — bet often, learn fast


@dataclass
class TeamStats:
    """NBA team statistics for edge calculation."""
    team_id: int
    team_name: str
    team_abbr: str
    wins: int = 0
    losses: int = 0
    win_pct: float = 0.0
    home_record: str = ""
    road_record: str = ""
    last_10: str = ""
    points_pg: float = 0.0
    opp_points_pg: float = 0.0
    diff_points_pg: float = 0.0
    current_streak: str = ""
    off_rating: float = 0.0
    def_rating: float = 0.0
    net_rating: float = 0.0
    pace: float = 0.0
    last_10_wins: int = 0
    last_10_losses: int = 0
    home_wins: int = 0
    home_losses: int = 0
    road_wins: int = 0
    road_losses: int = 0
    rest_days: int = 1
    is_b2b: bool = False


@dataclass
class H2HRecord:
    """Head-to-head record between two teams."""
    team_a_id: int
    team_b_id: int
    team_a_wins: int = 0
    team_b_wins: int = 0
    team_a_avg_pts: float = 0.0
    team_b_avg_pts: float = 0.0


@dataclass
class ResearchData:
    """All research data for an edge calculation."""
    home_team: TeamStats
    away_team: TeamStats
    h2h: Optional[H2HRecord] = None
    home_injuries: list[str] = field(default_factory=list)
    away_injuries: list[str] = field(default_factory=list)


@dataclass
class EdgeResult:
    """Result of an edge calculation."""
    market: Market
    our_fair_price: float
    market_price: float
    edge: float
    confidence: Confidence
    side: str  # "YES" or "NO"
    side_index: int  # 0 or 1 — index into outcomes
    research: Optional[ResearchData] = None
    has_vegas_line: bool = False   # Was a Vegas line available for this game?
    vegas_agrees: bool = False     # Does Vegas agree with our bet direction?

    @property
    def has_edge(self) -> bool:
        return self.edge >= self.market.min_edge


@dataclass
class Position:
    """A tracked position (open or closed)."""
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
    status: str = "open"
    game_start_time: Optional[str] = None
    market_end_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl: Optional[float] = None
    exit_reason: Optional[str] = None
    market_slug: str = ""
    fees_paid: float = 0.0  # Total Polymarket taker fees (entry + exit)

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
    order_id: str = ""
    pnl: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Trade:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
