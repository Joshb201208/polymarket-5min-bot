"""Options module Pydantic models.

Defines data structures for options contracts, open positions, trade signals,
and strategy types used throughout the options trading module.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Strategy enum ────────────────────────────────────────────────────

class OptionsStrategy(str, Enum):
    """Supported options strategies."""
    COVERED_CALL = "COVERED_CALL"
    CASH_SECURED_PUT = "CASH_SECURED_PUT"
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
    BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"


class OptionType(str, Enum):
    """Call or put."""
    CALL = "call"
    PUT = "put"


class OptionSide(str, Enum):
    """Order side for options."""
    BUY = "buy"
    SELL = "sell"


class OptionStyle(str, Enum):
    """American or European exercise."""
    AMERICAN = "american"
    EUROPEAN = "european"


class OptionOrderType(str, Enum):
    """Order type for options orders."""
    MARKET = "market"
    LIMIT = "limit"


# ── Core contract model ──────────────────────────────────────────────

class OptionsContract(BaseModel):
    """Options contract as returned by the Alpaca contracts API.

    Maps directly to the JSON response from
    GET /v2/options/contracts.
    """
    id: str
    symbol: str                         # e.g. AAPL260418C00255000
    name: str                           # e.g. AAPL Apr 18 2026 255 Call
    status: str = "active"
    tradable: bool = True
    expiration_date: date
    root_symbol: str
    underlying_symbol: str
    type: OptionType
    style: OptionStyle = OptionStyle.AMERICAN
    strike_price: float
    multiplier: int = 100               # Standard = 100 shares per contract
    size: int = 100
    close_price: Optional[float] = None


# ── Greeks model ─────────────────────────────────────────────────────

class OptionsGreeks(BaseModel):
    """Option greeks as reported by the market data snapshot."""
    delta: Optional[float] = None       # Sensitivity to underlying price
    gamma: Optional[float] = None       # Rate of change of delta
    theta: Optional[float] = None       # Daily time decay (negative for long)
    vega: Optional[float] = None        # Sensitivity to implied volatility
    rho: Optional[float] = None         # Sensitivity to interest rates
    implied_volatility: Optional[float] = None  # Annualised IV


# ── Market data snapshot ─────────────────────────────────────────────

class OptionsQuote(BaseModel):
    """Live quote and greeks for an options contract.

    Sourced from GET /v1beta1/options/snapshots on data.alpaca.markets.
    """
    symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    mid: Optional[float] = None         # Computed as (bid + ask) / 2
    close: Optional[float] = None       # Previous close (fallback when live data unavailable)
    open_interest: Optional[int] = None
    volume: Optional[int] = None
    greeks: OptionsGreeks = Field(default_factory=OptionsGreeks)
    updated_at: Optional[datetime] = None

    @property
    def fair_value(self) -> Optional[float]:
        """Return mid-price when available, else last, else close."""
        if self.mid is not None and self.mid > 0:
            return self.mid
        if self.last is not None and self.last > 0:
            return self.last
        if self.close is not None and self.close > 0:
            return self.close
        return None


# ── Open position ────────────────────────────────────────────────────

class OptionsPosition(BaseModel):
    """Tracks an open options position with live greeks and P&L.

    Persisted to disk as part of options_portfolio.json.
    """
    # Identity
    position_id: str                    # UUID generated at entry
    strategy: OptionsStrategy
    underlying_symbol: str

    # Leg 1 (always populated)
    contract_symbol: str                # Alpaca options contract symbol
    contract_id: str = ""               # Alpaca internal contract ID
    option_type: OptionType
    side: OptionSide                    # Whether we bought or sold this leg
    strike: float
    expiration_date: date
    multiplier: int = 100
    qty: int = 1                        # Number of contracts

    # Leg 2 (for spreads only)
    leg2_contract_symbol: Optional[str] = None
    leg2_contract_id: str = ""
    leg2_option_type: Optional[OptionType] = None
    leg2_side: Optional[OptionSide] = None
    leg2_strike: Optional[float] = None
    leg2_qty: int = 1

    # Entry / current pricing
    entry_price: float                  # Per-share price at entry (debit or credit)
    current_price: float = 0.0         # Per-share midpoint currently
    entry_cost: float = 0.0            # Total cash flow at entry (negative = credit received)

    # P&L
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    realized_pnl: float = 0.0

    # Greeks (position-level, scaled by qty * multiplier)
    greeks: OptionsGreeks = Field(default_factory=OptionsGreeks)

    # Lifecycle metadata
    opened_at: datetime
    closed_at: Optional[datetime] = None
    is_open: bool = True
    close_reason: Optional[str] = None  # e.g. "50% profit", "3 DTE", "manual"

    # Risk context
    max_profit: Optional[float] = None  # For spreads: strike_width - debit
    max_loss: Optional[float] = None    # For spreads: debit paid
    regime_at_entry: str = ""           # Macro regime when position was opened

    @property
    def dte(self) -> int:
        """Days to expiration from today."""
        today = datetime.utcnow().date()
        return max(0, (self.expiration_date - today).days)

    @property
    def should_auto_close(self) -> bool:
        """Return True if auto-close rules are triggered."""
        # Auto-close at 50% profit
        if self.unrealized_pnl_pct >= 50.0:
            return True
        # Auto-close at 3 DTE
        if self.dte <= 3:
            return True
        return False

    @property
    def close_trigger_reason(self) -> Optional[str]:
        """Human-readable reason for auto-close, or None."""
        if self.unrealized_pnl_pct >= 50.0:
            return "50% profit target reached"
        if self.dte <= 3:
            return f"3 DTE threshold reached ({self.dte} DTE remaining)"
        return None


# ── Trade signal ─────────────────────────────────────────────────────

class OptionsSignal(BaseModel):
    """A signal to open a new options position.

    Generated by the options engine and consumed by the executor.
    """
    strategy: OptionsStrategy
    underlying_symbol: str
    underlying_price: float             # Spot price at signal generation

    # Primary leg
    contract_symbol: Optional[str] = None   # Set after contract selection
    option_type: OptionType
    side: OptionSide
    target_strike: float                # Ideal strike price
    target_expiry_min_dte: int = 14     # Minimum acceptable DTE
    target_expiry_max_dte: int = 45     # Maximum acceptable DTE

    # Spread leg (for BULL_CALL_SPREAD / BEAR_PUT_SPREAD)
    leg2_contract_symbol: Optional[str] = None
    leg2_option_type: Optional[OptionType] = None
    leg2_side: Optional[OptionSide] = None
    leg2_target_strike: Optional[float] = None

    # Risk / sizing
    qty: int = 1
    max_debit_per_contract: Optional[float] = None   # Max we'll pay (limit)
    min_credit_per_contract: Optional[float] = None  # Min we'll accept (for sells)
    position_size_pct: float = 0.03     # % of portfolio to risk

    # Rationale
    rationale: str = ""
    conviction: int = 7                 # 1-10 from screener
    regime: str = ""                    # Macro regime that triggered signal
    generated_at: datetime = Field(default_factory=datetime.utcnow)

    # Computed risk metrics
    estimated_max_profit: Optional[float] = None
    estimated_max_loss: Optional[float] = None
    reward_to_risk: Optional[float] = None          # Minimum 2.0 for spreads


# ── Portfolio summary ─────────────────────────────────────────────────

class OptionsPortfolioState(BaseModel):
    """Persisted state of the entire options sub-portfolio."""
    positions: list[OptionsPosition] = Field(default_factory=list)
    closed_positions: list[OptionsPosition] = Field(default_factory=list)
    total_options_value: float = 0.0    # Current market value of all open positions
    total_options_exposure_pct: float = 0.0  # As % of total portfolio
    total_unrealized_pnl: float = 0.0
    total_realized_pnl: float = 0.0
    portfolio_delta: float = 0.0        # Aggregate delta (directional exposure)
    portfolio_theta: float = 0.0        # Daily theta income/decay
    last_updated: Optional[datetime] = None
