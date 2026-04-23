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
        conviction: int = 7,
    ) -> tuple[bool, str]:
        """Check whether a new or additional position is allowed.

        Returns (True, "ADD_TO_EXISTING") when topping up an existing
        position that is still below its effective target weight, or
        (True, "OK") for a new position.

        A hard per-symbol ceiling (config.MAX_POSITION_PCT) is always
        enforced — no new shares are acquired once a symbol crosses it,
        even if conviction is high.
        """
        existing = None
        for p in state.positions:
            if p.symbol == symbol:
                existing = p
                break

        portfolio_value = state.cash + sum(
            p.current_price * p.shares for p in state.positions
        )
        if portfolio_value <= 0:
            return False, "Portfolio value is zero or negative"

        hard_cap = self.config.MAX_POSITION_PCT
        conviction_target = self.config.CONVICTION_SIZE_MAP.get(
            conviction, self.config.CONVICTION_SIZE_MAP.get(7, 0.05)
        )
        # Effective target is the smaller of conviction-based size and hard cap.
        effective_target = min(conviction_target, hard_cap)

        if existing:
            current_weight = (existing.current_price * existing.shares) / portfolio_value
            # Hard cap check first — blocks additions even if conviction jumps.
            if current_weight >= hard_cap:
                return False, (
                    f"{symbol} at {current_weight:.1%} ≥ hard cap {hard_cap:.0%} — no adds"
                )
            if current_weight >= effective_target * 0.9:
                return False, (
                    f"Already holding {symbol} at {current_weight:.1%} "
                    f"(target {effective_target:.0%})"
                )
            return True, "ADD_TO_EXISTING"

        # 2. Position count (only for NEW positions, not add-ons)
        if len(state.positions) >= self.config.MAX_POSITIONS:
            return False, f"Max positions ({self.config.MAX_POSITIONS}) reached"

        # 3. Total exposure
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
        """Calculate number of shares using conviction-tiered sizing.

        Conviction → portfolio %:
            7 → 5%
            8 → 7%
            9 → 8%
            10 → 10%
        """
        if price <= 0 or portfolio_value <= 0:
            return 0

        # Look up conviction tier; default to minimum 3% for conviction 7
        size_pct = self.config.CONVICTION_SIZE_MAP.get(
            conviction, self.config.CONVICTION_SIZE_MAP.get(7, 0.03)
        )
        dollar_amount = portfolio_value * size_pct
        shares = int(dollar_amount / price)

        return max(shares, 0)

    def get_stop_loss_price(self, entry_price: float, beta: float | None = None) -> float:
        """Calculate volatility-adjusted stop-loss price.

        Formula: stop_pct = beta * 5%, clamped to [5%, 10%].
        If beta is not available, defaults to 5%.
        """
        if beta is not None and beta > 0:
            stop_pct = beta * 0.05
            # Clamp to configured range
            stop_pct = max(self.config.STOP_LOSS_PCT_MIN, min(stop_pct, self.config.STOP_LOSS_PCT_MAX))
        else:
            stop_pct = self.config.STOP_LOSS_PCT_MIN  # Default 5%

        return round(entry_price * (1 - stop_pct), 2)

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

    def should_stop_loss(self, entry_price: float, current_price: float, beta: float | None = None) -> bool:
        """Check if current price has breached stop-loss level."""
        stop = self.get_stop_loss_price(entry_price, beta)
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
            # Use beta from thesis if available
            beta = getattr(pos.thesis, "beta", None) if pos.thesis else None
            if self.should_stop_loss(pos.entry_price, pos.current_price, beta):
                alerts.append({
                    "symbol": pos.symbol,
                    "action": "STOP_LOSS",
                    "entry_price": pos.entry_price,
                    "current_price": pos.current_price,
                    "loss_pct": (pos.current_price - pos.entry_price) / pos.entry_price,
                    "reason": f"Stop-loss triggered: ${pos.current_price:.2f} <= ${self.get_stop_loss_price(pos.entry_price, beta):.2f}",
                })
        return alerts
