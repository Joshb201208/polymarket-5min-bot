import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from stock_agent.config import Config
from stock_agent.models import (
    DailySummary,
    PortfolioState,
    Position,
    Trade,
)

logger = logging.getLogger(__name__)


class Portfolio:
    def __init__(self, config: Config):
        self.config = config
        self._state_path = config.DATA_DIR / "portfolio.json"
        self.state: PortfolioState = self._load()

    def _load(self) -> PortfolioState:
        """Load portfolio state from disk."""
        if self._state_path.exists():
            try:
                raw = self._state_path.read_text()
                return PortfolioState.model_validate_json(raw)
            except Exception as e:
                logger.error("Failed to load portfolio state: %s", e)
                # Try backup
                backup = self._state_path.with_suffix(".json.bak")
                if backup.exists():
                    try:
                        raw = backup.read_text()
                        return PortfolioState.model_validate_json(raw)
                    except Exception:
                        pass
        logger.info("Starting with fresh portfolio state")
        return PortfolioState()

    def _save(self):
        """Atomic write: write to temp file then rename."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.state.model_dump_json(indent=2)

        # Write to temp file in same directory, then rename
        fd, tmp_path = tempfile.mkstemp(
            dir=self._state_path.parent, suffix=".tmp", prefix="portfolio_"
        )
        try:
            os.write(fd, data.encode())
            os.close(fd)

            # Backup current file
            if self._state_path.exists():
                backup = self._state_path.with_suffix(".json.bak")
                try:
                    backup.write_text(self._state_path.read_text())
                except Exception:
                    pass

            os.replace(tmp_path, self._state_path)
            logger.debug("Portfolio state saved to %s", self._state_path)
        except Exception as e:
            logger.error("Failed to save portfolio state: %s", e)
            try:
                os.close(fd)
            except Exception:
                pass
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def log_trade(self, trade: Trade):
        """Record a trade and update positions."""
        self.state.trade_history.append(trade)

        if trade.action == "BUY":
            # Position should already be added via add_position
            pass
        elif trade.action == "SELL":
            self.state.positions = [
                p for p in self.state.positions if p.symbol != trade.symbol
            ]
            # Update cash
            self.state.cash += trade.price * trade.shares

        self._save()

    def add_position(self, position: Position):
        """Add a new position and debit cash."""
        cost = position.entry_price * position.shares
        self.state.cash -= cost
        self.state.positions.append(position)
        self._save()

    def remove_position(self, symbol: str) -> Position | None:
        """Remove and return a position by symbol."""
        for i, p in enumerate(self.state.positions):
            if p.symbol == symbol:
                return self.state.positions.pop(i)
        return None

    def update_prices(self, prices: dict[str, dict]):
        """Update all position current prices and P&L from a prices dict."""
        for pos in self.state.positions:
            if pos.symbol in prices:
                quote = prices[pos.symbol]
                pos.current_price = quote.get("price", pos.current_price)
                pos.market_value = pos.current_price * pos.shares
                pos.unrealized_pnl = (pos.current_price - pos.entry_price) * pos.shares
                if pos.entry_price > 0:
                    pos.unrealized_pnl_pct = (pos.current_price - pos.entry_price) / pos.entry_price
                pos.last_updated = datetime.now(timezone.utc)
        self._save()

    def sync_from_alpaca(self, alpaca_positions: list[dict], alpaca_account: dict | None = None):
        """Make Alpaca the source of truth for shares, prices, P&L and cash.

        alpaca_positions: list from GET /v2/positions
        alpaca_account:   dict from GET /v2/account

        Options symbols (OCC format, len > 5) are skipped — the equity
        portfolio only tracks stocks. Options live in options_portfolio.
        """
        from stock_agent.models import Thesis  # local import to avoid cycle

        def _is_equity(sym: str) -> bool:
            # Equity tickers are <=5 uppercase letters (+ optional . suffix).
            # OCC option symbols ("TSM260424C00375000") are much longer.
            return bool(sym) and len(sym) <= 5 and sym.replace(".", "").isalpha()

        alpaca_by_sym = {
            p["symbol"]: p for p in alpaca_positions if _is_equity(p.get("symbol", ""))
        }

        now = datetime.now(timezone.utc)
        updated = 0
        imported = 0
        dropped = 0

        # 1. Update existing positions with Alpaca truth (shares, price, P&L).
        kept: list[Position] = []
        for pos in self.state.positions:
            ap = alpaca_by_sym.pop(pos.symbol, None)
            if not ap:
                # Position on our books but not at Alpaca — treat as closed.
                logger.warning(
                    "sync_from_alpaca: %s not found at Alpaca, dropping from local state",
                    pos.symbol,
                )
                dropped += 1
                continue
            cur_price = float(ap.get("current_price", 0) or 0)
            shares    = int(float(ap.get("qty", 0) or 0))
            avg_entry = float(ap.get("avg_entry_price", pos.entry_price) or pos.entry_price)
            if shares <= 0:
                dropped += 1
                continue
            pos.shares        = shares
            pos.entry_price   = avg_entry
            if cur_price > 0:
                pos.current_price      = cur_price
                pos.market_value       = float(ap.get("market_value", cur_price * shares) or 0)
                pos.unrealized_pnl     = float(ap.get("unrealized_pl", 0) or 0)
                pos.unrealized_pnl_pct = float(ap.get("unrealized_plpc", 0) or 0)
            pos.last_updated = now
            kept.append(pos)
            updated += 1

        # 2. Import Alpaca positions that the bot had no local record of.
        for sym, ap in alpaca_by_sym.items():
            try:
                shares    = int(float(ap.get("qty", 0) or 0))
                avg_entry = float(ap.get("avg_entry_price", 0) or 0)
                cur_price = float(ap.get("current_price", 0) or 0)
                if shares <= 0 or avg_entry <= 0:
                    continue
                placeholder_thesis = Thesis(
                    symbol=sym,
                    direction="HOLD",
                    conviction=7,
                    summary="Imported from Alpaca — awaiting re-analysis.",
                    bull_case="",
                    bear_case="",
                    catalysts=[],
                    risks=[],
                    generated_at=now,
                )
                imported_pos = Position(
                    symbol=sym,
                    shares=shares,
                    entry_price=avg_entry,
                    entry_date=now,
                    current_price=cur_price,
                    market_value=float(ap.get("market_value", cur_price * shares) or 0),
                    unrealized_pnl=float(ap.get("unrealized_pl", 0) or 0),
                    unrealized_pnl_pct=float(ap.get("unrealized_plpc", 0) or 0),
                    stop_loss=round(avg_entry * 0.95, 2),
                    thesis=placeholder_thesis,
                    sector="Unknown",
                    last_updated=now,
                )
                kept.append(imported_pos)
                imported += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("sync_from_alpaca: failed to import %s: %s", sym, exc)

        self.state.positions = kept

        # 3. Sync cash — always, including negative (margin) balances.
        if alpaca_account:
            try:
                self.state.cash = float(alpaca_account.get("cash", self.state.cash))
            except (TypeError, ValueError):
                pass

        self._save()
        logger.info(
            "Portfolio synced from Alpaca — updated=%d imported=%d dropped=%d cash=$%.0f",
            updated, imported, dropped, self.state.cash,
        )

    def get_portfolio_value(self) -> float:
        """Total portfolio value: cash + market value of all positions."""
        market_val = sum(p.current_price * p.shares for p in self.state.positions)
        return self.state.cash + market_val

    def get_total_unrealized_pnl(self) -> tuple[float, float]:
        """Total unrealized P&L across all positions.

        Returns (dollar_pnl, pct_pnl).
        """
        total_cost = sum(p.entry_price * p.shares for p in self.state.positions)
        total_market = sum(p.current_price * p.shares for p in self.state.positions)
        dollar_pnl = total_market - total_cost
        pct_pnl = dollar_pnl / total_cost if total_cost > 0 else 0.0
        return dollar_pnl, pct_pnl

    def get_exposure(self) -> float:
        """Total exposure as fraction of portfolio value."""
        pv = self.get_portfolio_value()
        if pv <= 0:
            return 0.0
        market_val = sum(p.current_price * p.shares for p in self.state.positions)
        return market_val / pv

    def get_sector_exposure(self) -> dict[str, float]:
        """Sector exposure as fraction of portfolio value."""
        pv = self.get_portfolio_value()
        if pv <= 0:
            return {}
        sectors: dict[str, float] = {}
        for p in self.state.positions:
            val = p.current_price * p.shares
            sectors[p.sector] = sectors.get(p.sector, 0) + val / pv
        return sectors

    def get_position(self, symbol: str) -> Position | None:
        for p in self.state.positions:
            if p.symbol == symbol:
                return p
        return None

    def get_equity_curve(self) -> list[dict]:
        """Return equity curve from daily summaries."""
        return [
            {"date": s.date, "value": s.portfolio_value}
            for s in self.state.daily_summaries
        ]

    def calculate_stats(self) -> dict:
        """Calculate performance statistics."""
        trades = [t for t in self.state.trade_history if t.action == "SELL" and t.pnl is not None]

        # Include unrealized P&L in stats
        unrealized_pnl, unrealized_pnl_pct = self.get_total_unrealized_pnl()
        pv = self.get_portfolio_value()
        total_return = pv - self.state.starting_capital
        total_return_pct = total_return / self.state.starting_capital if self.state.starting_capital > 0 else 0

        if not trades:
            return {
                "total_trades": len(self.state.trade_history),
                "closed_trades": 0,
                "win_rate": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": unrealized_pnl,
                "total_pnl": unrealized_pnl,
                "total_return": total_return,
                "total_return_pct": total_return_pct,
                "avg_pnl_pct": 0.0,
                "max_drawdown": 0.0,
                "sharpe": 0.0,
            }

        wins = [t for t in trades if t.pnl and t.pnl > 0]
        realized_pnl = sum(t.pnl for t in trades if t.pnl)
        pnl_pcts = [t.pnl_pct for t in trades if t.pnl_pct is not None]
        avg_pnl_pct = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0

        # Sharpe (simplified — annualized from daily summaries)
        sharpe = 0.0
        if len(self.state.daily_summaries) >= 2:
            returns = []
            summaries = self.state.daily_summaries
            for i in range(1, len(summaries)):
                if summaries[i - 1].portfolio_value > 0:
                    r = (summaries[i].portfolio_value - summaries[i - 1].portfolio_value) / summaries[i - 1].portfolio_value
                    returns.append(r)
            if returns:
                import statistics
                mean_r = statistics.mean(returns)
                std_r = statistics.stdev(returns) if len(returns) > 1 else 1
                if std_r > 0:
                    sharpe = (mean_r / std_r) * (252 ** 0.5)

        # Max drawdown
        max_dd = 0.0
        peak = 0.0
        for s in self.state.daily_summaries:
            if s.portfolio_value > peak:
                peak = s.portfolio_value
            if peak > 0:
                dd = (peak - s.portfolio_value) / peak
                max_dd = max(max_dd, dd)

        return {
            "total_trades": len(self.state.trade_history),
            "closed_trades": len(trades),
            "win_rate": len(wins) / len(trades) if trades else 0,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": realized_pnl + unrealized_pnl,
            "total_return": total_return,
            "total_return_pct": total_return_pct,
            "avg_pnl_pct": avg_pnl_pct,
            "max_drawdown": max_dd,
            "sharpe": sharpe,
        }

    def add_daily_summary(self, summary: DailySummary):
        """Append a daily summary."""
        self.state.daily_summaries.append(summary)
        self._save()

    def update_thesis(self, symbol: str, thesis):
        """Store an active thesis keyed by symbol."""
        self.state.active_theses[symbol] = thesis.model_dump() if hasattr(thesis, "model_dump") else thesis
        self._save()

    def set_last_weekly_analysis(self, dt: datetime):
        self.state.last_weekly_analysis = dt
        self._save()

    def set_last_daily_check(self, dt: datetime):
        self.state.last_daily_check = dt
        self._save()

    def set_universe(self, symbols: list[str]):
        self.state.universe = symbols
        self.state.last_universe_refresh = datetime.now(timezone.utc)
        self._save()
