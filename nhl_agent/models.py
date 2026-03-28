"""Data models for the NHL betting agent."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class NHLMarketType(str, Enum):
    MONEYLINE = "moneyline"
    FUTURES = "futures"
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
class NHLMarket:
    """A Polymarket NHL market."""
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
    market_type: NHLMarketType = NHLMarketType.UNKNOWN
    event_slug: str = ""
    event_title: str = ""

    @classmethod
    def from_api(cls, raw: dict, event_slug: str = "", event_title: str = "") -> NHLMarket:
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

    def detect_market_type(self) -> NHLMarketType:
        slug = self.slug.lower()
        event = self.event_slug.lower()
        combined = slug + " " + event

        # NHL futures: Stanley Cup, Hart Trophy, Calder Trophy, etc.
        _FUTURES_KEYWORDS = ("stanley-cup", "champion", "trophy", "winner", "mvp")
        if any(kw in combined for kw in _FUTURES_KEYWORDS):
            return NHLMarketType.FUTURES

        # NHL moneyline: nhl-{away}-{home}-{date}
        if slug.startswith("nhl-"):
            parts = slug.split("-")
            if len(parts) >= 6:
                try:
                    int(parts[-3])
                    int(parts[-2])
                    int(parts[-1])
                    return NHLMarketType.MONEYLINE
                except (ValueError, IndexError):
                    pass
        return NHLMarketType.UNKNOWN

    @property
    def is_game_market(self) -> bool:
        return self.market_type == NHLMarketType.MONEYLINE

    @property
    def is_futures_market(self) -> bool:
        return self.market_type == NHLMarketType.FUTURES

    @property
    def min_edge(self) -> float:
        if self.market_type == NHLMarketType.FUTURES:
            return 0.06  # 6% for futures (capital locked longer)
        return 0.04  # 4% for game markets


@dataclass
class NHLTeamStats:
    """NHL team statistics for edge calculation."""
    team_name: str
    team_abbr: str
    wins: int = 0
    losses: int = 0
    ot_losses: int = 0
    points: int = 0
    win_pct: float = 0.0
    home_record: str = ""
    road_record: str = ""
    last_10: str = ""
    goals_pg: float = 0.0
    goals_against_pg: float = 0.0
    goal_diff_pg: float = 0.0
    current_streak: str = ""
    last_10_wins: int = 0
    last_10_losses: int = 0
    home_wins: int = 0
    home_losses: int = 0
    road_wins: int = 0
    road_losses: int = 0
    rest_days: int = 1
    is_b2b: bool = False
    # Advanced stats (MoneyPuck)
    xgf_pct: float = 0.0   # Expected goals for %
    corsi_pct: float = 0.0  # Corsi for %
    fenwick_pct: float = 0.0
    pdo: float = 100.0
    pp_pct: float = 0.0   # Power play %
    pk_pct: float = 0.0   # Penalty kill %


@dataclass
class NHLH2HRecord:
    """Head-to-head record between two NHL teams."""
    team_a: str
    team_b: str
    team_a_wins: int = 0
    team_b_wins: int = 0


@dataclass
class NHLResearchData:
    """All research data for an NHL edge calculation."""
    home_team: NHLTeamStats
    away_team: NHLTeamStats
    h2h: Optional[NHLH2HRecord] = None
    home_injuries: list[str] = field(default_factory=list)
    away_injuries: list[str] = field(default_factory=list)


@dataclass
class NHLEdgeResult:
    """Result of an NHL edge calculation."""
    market: NHLMarket
    our_fair_price: float
    market_price: float
    edge: float
    confidence: Confidence
    side: str  # "YES" or "NO"
    side_index: int  # 0 or 1
    research: Optional[NHLResearchData] = None
    has_vegas_line: bool = False
    vegas_agrees: bool = False

    @property
    def has_edge(self) -> bool:
        return self.edge >= self.market.min_edge


@dataclass
class NHLPosition:
    """A tracked NHL position."""
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
    market_type: str = "moneyline"
    fees_paid: float = 0.0
    hours_before_faceoff: Optional[float] = None
    opponent_win_pct: Optional[float] = None
    price_at_gametime: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> NHLPosition:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class NHLTrade:
    """A completed NHL trade for logging."""
    id: str
    position_id: str
    market_id: str
    market_question: str
    action: str
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
    def from_dict(cls, d: dict) -> NHLTrade:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
