from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class TipRanksStock(BaseModel):
    """TipRanks screener data for a single stock."""
    symbol: str
    name: str
    smart_score: int
    analyst_consensus: str  # Strong Buy, Moderate Buy, Hold, etc.
    analyst_target_upside: float  # percentage
    hedge_fund_signal: str  # Positive, Negative, Neutral
    insider_signal: str  # Positive, Negative, Neutral
    news_sentiment: str  # Very Bullish, Bullish, Neutral, Bearish
    blogger_consensus: str
    ai_rating: Optional[str] = None
    ai_target_upside: Optional[float] = None
    sector: str
    market_cap: Optional[str] = None
    pe_ratio: Optional[float] = None


class CompanyData(BaseModel):
    """Raw financial data for a company."""
    symbol: str
    name: str
    sector: str
    industry: str
    market_cap: float
    price: float
    pe_ratio: Optional[float] = None
    forward_pe: Optional[float] = None
    pb_ratio: Optional[float] = None
    ps_ratio: Optional[float] = None
    ev_ebitda: Optional[float] = None
    revenue_growth_yoy: Optional[float] = None
    earnings_growth_yoy: Optional[float] = None
    gross_margin: Optional[float] = None
    operating_margin: Optional[float] = None
    net_margin: Optional[float] = None
    roe: Optional[float] = None
    roic: Optional[float] = None
    fcf_yield: Optional[float] = None
    debt_to_equity: Optional[float] = None
    current_ratio: Optional[float] = None
    revenue_ttm: Optional[float] = None
    net_income_ttm: Optional[float] = None
    fcf_ttm: Optional[float] = None
    analyst_target_price: Optional[float] = None
    analyst_rating: Optional[str] = None
    dcf_value: Optional[float] = None
    next_earnings_date: Optional[str] = None
    revenue_segments: Optional[dict] = None
    peers: Optional[list[str]] = None


class Thesis(BaseModel):
    """Investment thesis for a stock."""
    symbol: str
    direction: str  # "BUY" or "SELL" or "HOLD"
    conviction: int  # 1-10
    summary: str
    bull_case: str
    bear_case: str
    catalysts: list[str]
    risks: list[str]
    target_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    time_horizon: str = "4-8 weeks"
    sources: list[str] = []
    generated_at: datetime


class Signal(BaseModel):
    """Trading signal."""
    symbol: str
    action: str  # "BUY", "SELL", "HOLD"
    conviction: int  # 1-10
    thesis: Thesis
    entry_price: Optional[float] = None
    position_size_pct: float
    generated_at: datetime


class Position(BaseModel):
    """Open position."""
    symbol: str
    shares: int
    entry_price: float
    entry_date: datetime
    current_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    stop_loss: float
    thesis: Thesis
    sector: str
    last_updated: datetime


class Trade(BaseModel):
    """Completed trade record."""
    symbol: str
    action: str  # "BUY" or "SELL"
    shares: int
    price: float
    timestamp: datetime
    reason: str
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    hold_days: Optional[int] = None


class DailySummary(BaseModel):
    """Daily portfolio summary."""
    date: str
    portfolio_value: float
    cash: float
    total_exposure: float
    exposure_pct: float
    num_positions: int
    day_pnl: float
    day_pnl_pct: float
    total_pnl: float
    total_pnl_pct: float
    trades_today: list[Trade]
    positions: list[Position]
    signals: list[Signal]


class PortfolioState(BaseModel):
    """Complete portfolio state — persisted to JSON."""
    positions: list[Position] = []
    trade_history: list[Trade] = []
    daily_summaries: list[DailySummary] = []
    cash: float = 100000.0
    starting_capital: float = 100000.0
    last_universe_refresh: Optional[datetime] = None
    universe: list[str] = []
    active_theses: dict = {}
    last_weekly_analysis: Optional[datetime] = None
    last_daily_check: Optional[datetime] = None
