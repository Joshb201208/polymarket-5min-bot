import logging
from datetime import datetime, timedelta, timezone

from stock_agent.config import Config
from stock_agent.models import PortfolioState, Trade

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, config: Config):
        self.config = config

    def can_open_position(
        self,
        state: PortfolioState,
        symbol: str,
        sector: str,
        price: float,
    ) -> tuple[bool, str]:
        """Check whether a new position is allowed under all risk rules."""
        # 1. Duplicate check
        for p in state.positions:
            if p.symbol == symbol:
                return False, f"Already holding {symbol}"

        # 2. Position count
        if len(state.positions) >= self.config.MAX_POSITIONS:
            return False, f"Max positions ({self.config.MAX_POSITIONS}) reached"

        # 3. Total exposure
        portfolio_value = state.cash + sum(
            p.current_price * p.shares for p in state.positions
        )
        if portfolio_value <= 0:
            return False, "Portfolio value is zero or negative"

        market_exposure = sum(p.current_price * p.shares for p in state.positions)
        exposure_pct = market_exposure / portfolio_value
        if exposure_pct >= self.config.MAX_TOTAL_EXPOSURE:
            return False, f"Total exposure {exposure_pct:.1%} >= limit {self.config.MAX_TOTAL_EXPOSURE:.0%}"

        # 4. Sector concentration
        sector_val = sum(
            p.current_price * p.shares
            for p in state.positions
            if p.sector == sector
        )
        sector_pct = sector_val / portfolio_value
        if sector_pct >= self.config.MAX_SECTOR_PCT:
            return False, f"Sector '{sector}' at {sector_pct:.1%} >= limit {self.config.MAX_SECTOR_PCT:.0%}"

        # 5. Enough cash for minimum order
        if state.cash < price:
            return False, f"Insufficient cash (${state.cash:,.0f}) for even 1 share at ${price:.2f}"

        return True, "OK"

    def calculate_position_size(
        self,
        portfolio_value: float,
        price: float,
        conviction: int,
    ) -> int:
        """Calculate number of shares based on conviction and risk limits.

        Base: 5% of portfolio / price = max shares.
        Scale by conviction: 7 = 70%, 8 = 80%, 9 = 90%, 10 = 100%.
        """
        if price <= 0 or portfolio_value <= 0:
            return 0

        max_dollar = portfolio_value * self.config.MAX_POSITION_PCT
        conviction_scale = conviction / 10.0
        dollar_amount = max_dollar * conviction_scale
        shares = int(dollar_amount / price)

        return max(shares, 0)

    def get_stop_loss_price(self, entry_price: float) -> float:
        """Calculate stop-loss price: 5% below entry."""
        return round(entry_price * (1 - self.config.STOP_LOSS_PCT), 2)

    def check_pdt_compliance(self, trade_history: list[Trade]) -> bool:
        """Check Pattern Day Trader rule: max 3 day trades per rolling 5 business days.

        A day trade = buying and selling the same security on the same day.
        """
        now = datetime.now(timezone.utc)
        five_days_ago = now - timedelta(days=7)  # 7 calendar days ≈ 5 business days

        recent_trades = [
            t for t in trade_history
            if t.timestamp >= five_days_ago
        ]

        # Find day trades: same symbol bought and sold on same calendar day
        day_trades = 0
        buys_by_day: dict[str, set[str]] = {}  # date_str -> set of symbols bought
        sells_by_day: dict[str, set[str]] = {}

        for t in recent_trades:
            day_key = t.timestamp.strftime("%Y-%m-%d")
            if t.action == "BUY":
                buys_by_day.setdefault(day_key, set()).add(t.symbol)
            elif t.action == "SELL":
                sells_by_day.setdefault(day_key, set()).add(t.symbol)

        for day_key, buy_syms in buys_by_day.items():
            sell_syms = sells_by_day.get(day_key, set())
            day_trades += len(buy_syms & sell_syms)

        compliant = day_trades < 3
        if not compliant:
            logger.warning("PDT limit reached: %d day trades in last 5 days", day_trades)
        return compliant

    def should_stop_loss(self, entry_price: float, current_price: float) -> bool:
        """Check if current price has breached stop-loss level."""
        stop = self.get_stop_loss_price(entry_price)
        return current_price <= stop

    def check_position_health(
        self,
        state: PortfolioState,
    ) -> list[dict]:
        """Check all positions for stop-loss breaches. Returns list of alerts."""
        alerts = []
        for pos in state.positions:
            if pos.current_price <= 0:
                continue
            if self.should_stop_loss(pos.entry_price, pos.current_price):
                alerts.append({
                    "symbol": pos.symbol,
                    "action": "STOP_LOSS",
                    "entry_price": pos.entry_price,
                    "current_price": pos.current_price,
                    "loss_pct": (pos.current_price - pos.entry_price) / pos.entry_price,
                    "reason": f"Stop-loss triggered: ${pos.current_price:.2f} <= ${self.get_stop_loss_price(pos.entry_price):.2f}",
                })
        return alerts
