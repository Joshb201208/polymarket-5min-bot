"""
scalp_paper_trader.py - Paper trading simulation with BUY and SELL support.

Unlike the original paper_trader.py which holds to binary expiry, this
module supports active selling of tokens back into the order book.

Key features:
  - simulate_buy()  — buy tokens (deduct from balance)
  - simulate_sell() — sell tokens back (add to balance minus fees)
  - Position tracking with entry_price, shares, entry_time
  - Sell price simulation from CLOB bid side with slippage
  - State persistence to scalp_paper_state.json
"""

import json
import os
import time
import logging
import threading
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from datetime import datetime, timezone

import httpx

from utils import polymarket_fee, round_to_tick, format_usd
from scalp_strategy import ScalpPosition

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------

@dataclass
class BuyResult:
    """Result of a simulate_buy() call."""
    success: bool
    position_id: str = ""
    filled_price: float = 0.0
    shares: float = 0.0
    size_usd: float = 0.0
    fee_paid: float = 0.0
    error: str = ""


@dataclass
class SellResult:
    """Result of a simulate_sell() call."""
    success: bool
    position_id: str = ""
    sell_price: float = 0.0
    shares_sold: float = 0.0
    proceeds_usd: float = 0.0
    fee_paid: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    pnl_pct: float = 0.0
    error: str = ""


@dataclass
class ClosedScalpTrade:
    """Record of a completed scalp trade (buy + sell)."""
    position_id: str
    asset: str
    direction: str
    token_id: str
    entry_price: float
    exit_price: float
    shares: float
    size_usd: float
    entry_fee: float
    exit_fee: float
    gross_pnl: float
    net_pnl: float
    pnl_pct: float
    hold_time_secs: float
    entry_time: float
    exit_time: float
    exit_reason: str
    balance_after: float


# ---------------------------------------------------------------------------
# ScalpPaperTrader
# ---------------------------------------------------------------------------

class ScalpPaperTrader:
    """
    Paper trading that supports both buying and selling tokens.

    Simulates fills using real CLOB orderbook data and tracks positions
    with entry/exit P&L.
    """

    CLOB_BASE = "https://clob.polymarket.com"
    SLIPPAGE_TICKS = 2  # ticks of slippage on sells (conservative)

    def __init__(
        self,
        initial_balance: float = 500.0,
        state_file: str = "scalp_paper_state.json",
    ):
        self._initial_balance = initial_balance
        self._balance = initial_balance
        self._state_file = state_file
        self._lock = threading.Lock()

        # Open positions: position_id -> ScalpPosition
        self._positions: Dict[str, ScalpPosition] = {}

        # Closed trade history
        self._closed_trades: List[ClosedScalpTrade] = []

        # Counter for position IDs
        self._position_counter = 0

        # Load persisted state
        self._load_state()

        logger.info(
            "ScalpPaperTrader: balance=%s, open_positions=%d, closed=%d",
            format_usd(self._balance),
            len(self._positions),
            len(self._closed_trades),
        )

    # ------------------------------------------------------------------
    # Buy simulation
    # ------------------------------------------------------------------

    def simulate_buy(
        self,
        market: dict,
        side: str,
        size_usd: float,
        price: float,
    ) -> BuyResult:
        """
        Simulate buying tokens on Polymarket.

        Uses real orderbook data to determine fill price. Deducts cost
        + fees from paper balance.

        Args:
            market:   Market dict from MarketFinder.
            side:     "YES" or "NO".
            size_usd: USD amount to spend.
            price:    Desired limit price (Polymarket probability).

        Returns:
            BuyResult with fill details.
        """
        if size_usd <= 0:
            return BuyResult(success=False, error="Invalid size")

        token_id = market.get("token_id_yes") if side == "YES" else market.get("token_id_no")
        if not token_id:
            return BuyResult(
                success=False,
                error=f"No token_id for {side} in market {market.get('slug')}",
            )

        # Fetch real orderbook ask price
        fill_price = self._get_ask_price(token_id, price)
        if fill_price is None:
            fill_price = price
            logger.debug(
                "ScalpPaper: using fallback buy price %.3f for %s",
                fill_price, token_id[:16],
            )

        # FOK semantics: reject if fill_price > limit + 2 ticks
        if fill_price > price + 0.02:
            return BuyResult(
                success=False,
                error=f"Ask {fill_price:.3f} > limit {price:.3f} + slippage",
            )

        shares = size_usd / fill_price if fill_price > 0 else 0
        fee = polymarket_fee(shares=shares, price=fill_price)
        total_cost = size_usd + fee

        with self._lock:
            if total_cost > self._balance:
                return BuyResult(
                    success=False,
                    error=f"Cost {format_usd(total_cost)} > balance {format_usd(self._balance)}",
                )

            self._balance -= total_cost
            self._position_counter += 1
            position_id = f"SCALP-{self._position_counter:06d}-{int(time.time())}"

            position = ScalpPosition(
                position_id=position_id,
                asset=market.get("asset", ""),
                direction=side,
                token_id=token_id,
                entry_price=fill_price,
                shares=shares,
                size_usd=size_usd,
                entry_time=time.time(),
                entry_fee=fee,
            )
            self._positions[position_id] = position

        self._save_state()

        logger.info(
            "ScalpPaper BUY: %s | %s %s at %.3f | shares=%.2f fee=%s | bal=%s",
            position_id, side, market.get("asset"), fill_price,
            shares, format_usd(fee), format_usd(self._balance),
        )

        return BuyResult(
            success=True,
            position_id=position_id,
            filled_price=fill_price,
            shares=shares,
            size_usd=size_usd,
            fee_paid=fee,
        )

    # ------------------------------------------------------------------
    # Sell simulation
    # ------------------------------------------------------------------

    def simulate_sell(
        self,
        position_id: str,
        exit_reason: str = "unknown",
    ) -> SellResult:
        """
        Simulate selling tokens back on Polymarket.

        Fetches real CLOB bid price and applies slippage to simulate
        realistic fill. Adds proceeds minus fees to paper balance.

        Args:
            position_id: ID of the position to close.
            exit_reason: Why we're exiting (for logging).

        Returns:
            SellResult with P&L details.
        """
        with self._lock:
            position = self._positions.get(position_id)
            if position is None:
                return SellResult(
                    success=False,
                    error=f"Position {position_id} not found",
                )

        # Fetch real bid price for sell simulation
        sell_price = self._get_bid_price(position.token_id)
        if sell_price is None:
            # Fallback: use entry price (conservative — assume no gain)
            sell_price = position.entry_price
            logger.debug(
                "ScalpPaper: using fallback sell price %.3f for %s",
                sell_price, position.token_id[:16],
            )

        # Calculate proceeds and P&L
        gross_proceeds = position.shares * sell_price
        exit_fee = polymarket_fee(shares=position.shares, price=sell_price)
        net_proceeds = gross_proceeds - exit_fee

        # P&L relative to what we paid
        gross_pnl = gross_proceeds - position.size_usd
        net_pnl = net_proceeds - position.size_usd - position.entry_fee
        pnl_pct = net_pnl / position.size_usd if position.size_usd > 0 else 0.0
        hold_time = time.time() - position.entry_time

        with self._lock:
            self._balance += net_proceeds
            balance_after = self._balance

            # Record closed trade
            closed = ClosedScalpTrade(
                position_id=position_id,
                asset=position.asset,
                direction=position.direction,
                token_id=position.token_id,
                entry_price=position.entry_price,
                exit_price=sell_price,
                shares=position.shares,
                size_usd=position.size_usd,
                entry_fee=position.entry_fee,
                exit_fee=exit_fee,
                gross_pnl=gross_pnl,
                net_pnl=net_pnl,
                pnl_pct=pnl_pct,
                hold_time_secs=hold_time,
                entry_time=position.entry_time,
                exit_time=time.time(),
                exit_reason=exit_reason,
                balance_after=balance_after,
            )
            self._closed_trades.append(closed)

            # Remove from open positions
            del self._positions[position_id]

        self._save_state()

        pnl_str = f"${net_pnl:+.2f} ({pnl_pct*100:+.1f}%)"
        logger.info(
            "ScalpPaper SELL: %s | %s %s | entry=%.3f exit=%.3f | %s | hold=%.0fs | %s | bal=%s",
            position_id, position.direction, position.asset,
            position.entry_price, sell_price, pnl_str,
            hold_time, exit_reason, format_usd(balance_after),
        )

        return SellResult(
            success=True,
            position_id=position_id,
            sell_price=sell_price,
            shares_sold=position.shares,
            proceeds_usd=net_proceeds,
            fee_paid=exit_fee,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            pnl_pct=pnl_pct,
        )

    # ------------------------------------------------------------------
    # Position accessors
    # ------------------------------------------------------------------

    def get_position(self, position_id: str) -> Optional[ScalpPosition]:
        """Get a specific open position."""
        with self._lock:
            return self._positions.get(position_id)

    def get_open_positions(self) -> List[ScalpPosition]:
        """Return all open positions."""
        with self._lock:
            return list(self._positions.values())

    def get_open_positions_for_asset(self, asset: str) -> List[ScalpPosition]:
        """Return open positions for a specific asset."""
        asset = asset.upper()
        with self._lock:
            return [p for p in self._positions.values() if p.asset == asset]

    def get_closed_trades(self) -> List[ClosedScalpTrade]:
        """Return all closed trade records."""
        with self._lock:
            return list(self._closed_trades)

    def get_balance(self) -> float:
        """Return current paper balance."""
        with self._lock:
            return self._balance

    def get_stats(self) -> dict:
        """Quick stats summary."""
        with self._lock:
            closed = list(self._closed_trades)
            balance = self._balance
            open_count = len(self._positions)

        total = len(closed)
        if total == 0:
            return {
                "total_trades": 0,
                "balance": balance,
                "open_positions": open_count,
                "pnl": balance - self._initial_balance,
            }

        wins = sum(1 for t in closed if t.net_pnl > 0)
        losses = total - wins
        total_pnl = sum(t.net_pnl for t in closed)
        avg_hold = sum(t.hold_time_secs for t in closed) / total
        avg_pnl_pct = sum(t.pnl_pct for t in closed) / total

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / total if total > 0 else 0,
            "total_pnl": total_pnl,
            "avg_pnl_pct": avg_pnl_pct,
            "avg_hold_seconds": avg_hold,
            "balance": balance,
            "open_positions": open_count,
            "return_pct": (balance - self._initial_balance) / self._initial_balance,
        }

    # ------------------------------------------------------------------
    # Price fetching
    # ------------------------------------------------------------------

    def _get_ask_price(self, token_id: str, desired_price: float) -> Optional[float]:
        """
        Get the best ask price from the CLOB orderbook (for buying).
        Falls back to CLOB midpoint + 1 tick if orderbook unavailable.
        """
        try:
            with httpx.Client(timeout=6) as client:
                resp = client.get(
                    f"{self.CLOB_BASE}/book",
                    params={"token_id": token_id},
                )
                if resp.status_code == 200:
                    book = resp.json()
                    asks = book.get("asks", [])
                    if asks:
                        best_ask = min(float(a.get("price", 1.0)) for a in asks)
                        return round_to_tick(best_ask)
        except Exception as exc:
            logger.debug("ScalpPaper: ask price error for %s: %s", token_id[:16], exc)

        # Try midpoint fallback
        try:
            with httpx.Client(timeout=6) as client:
                resp = client.get(
                    f"{self.CLOB_BASE}/midpoint",
                    params={"token_id": token_id},
                )
                if resp.status_code == 200:
                    mid = float(resp.json().get("mid", 0) or 0)
                    if mid > 0:
                        return round_to_tick(mid + 0.01)
        except Exception as exc:
            logger.debug("ScalpPaper: midpoint error for %s: %s", token_id[:16], exc)

        return None

    def _get_bid_price(self, token_id: str) -> Optional[float]:
        """
        Get the best bid price from the CLOB orderbook (for selling).
        Applies SLIPPAGE_TICKS of slippage for conservative fill estimation.
        Falls back to midpoint - 1 tick.
        """
        try:
            with httpx.Client(timeout=6) as client:
                resp = client.get(
                    f"{self.CLOB_BASE}/book",
                    params={"token_id": token_id},
                )
                if resp.status_code == 200:
                    book = resp.json()
                    bids = book.get("bids", [])
                    if bids:
                        best_bid = max(float(b.get("price", 0.0)) for b in bids)
                        # Apply slippage
                        slipped = best_bid - (self.SLIPPAGE_TICKS * 0.01)
                        return round_to_tick(max(slipped, 0.01))
        except Exception as exc:
            logger.debug("ScalpPaper: bid price error for %s: %s", token_id[:16], exc)

        # Try midpoint fallback
        try:
            with httpx.Client(timeout=6) as client:
                resp = client.get(
                    f"{self.CLOB_BASE}/midpoint",
                    params={"token_id": token_id},
                )
                if resp.status_code == 200:
                    mid = float(resp.json().get("mid", 0) or 0)
                    if mid > 0:
                        return round_to_tick(max(mid - 0.01, 0.01))
        except Exception as exc:
            logger.debug("ScalpPaper: midpoint error for %s: %s", token_id[:16], exc)

        return None

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Save state to JSON file."""
        try:
            with self._lock:
                closed = list(self._closed_trades)
                wins = sum(1 for t in closed if t.net_pnl > 0)
                total_pnl = sum(t.net_pnl for t in closed)

                state = {
                    "balance": self._balance,
                    "initial_balance": self._initial_balance,
                    "position_counter": self._position_counter,
                    "positions": {
                        pid: {
                            "position_id": p.position_id,
                            "asset": p.asset,
                            "direction": p.direction,
                            "token_id": p.token_id,
                            "entry_price": p.entry_price,
                            "shares": p.shares,
                            "size_usd": p.size_usd,
                            "entry_time": p.entry_time,
                            "entry_fee": p.entry_fee,
                        }
                        for pid, p in self._positions.items()
                    },
                    "closed_trades": [asdict(t) for t in closed[-200:]],
                    "stats": {
                        "total_trades": len(closed),
                        "wins": wins,
                        "losses": len(closed) - wins,
                        "total_pnl": total_pnl,
                    },
                    "updated": datetime.now(tz=timezone.utc).isoformat(),
                }

            with open(self._state_file, "w") as f:
                json.dump(state, f, indent=2, default=str)
        except Exception as exc:
            logger.warning("ScalpPaperTrader: could not save state: %s", exc)

    def _load_state(self) -> None:
        """Load persisted state."""
        if not os.path.exists(self._state_file):
            return

        try:
            with open(self._state_file) as f:
                state = json.load(f)

            with self._lock:
                self._balance = float(state.get("balance", self._initial_balance))
                self._position_counter = int(state.get("position_counter", 0))

                # Restore open positions
                for pid, pdata in state.get("positions", {}).items():
                    try:
                        self._positions[pid] = ScalpPosition(
                            position_id=pdata["position_id"],
                            asset=pdata["asset"],
                            direction=pdata["direction"],
                            token_id=pdata["token_id"],
                            entry_price=float(pdata["entry_price"]),
                            shares=float(pdata["shares"]),
                            size_usd=float(pdata["size_usd"]),
                            entry_time=float(pdata["entry_time"]),
                            entry_fee=float(pdata["entry_fee"]),
                        )
                    except (KeyError, ValueError, TypeError) as exc:
                        logger.warning(
                            "ScalpPaperTrader: skipping position %s: %s", pid, exc,
                        )

                # Restore closed trades
                for tdata in state.get("closed_trades", []):
                    try:
                        self._closed_trades.append(ClosedScalpTrade(**tdata))
                    except (TypeError, KeyError):
                        pass

            logger.info(
                "ScalpPaperTrader: loaded state — balance=%s, positions=%d, closed=%d",
                format_usd(self._balance),
                len(self._positions),
                len(self._closed_trades),
            )
        except Exception as exc:
            logger.warning("ScalpPaperTrader: could not load state: %s", exc)

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self, new_balance: float = 500.0) -> None:
        """Reset to fresh state."""
        with self._lock:
            self._balance = new_balance
            self._initial_balance = new_balance
            self._positions.clear()
            self._closed_trades.clear()
            self._position_counter = 0
        self._save_state()
        logger.info("ScalpPaperTrader: reset to %s", format_usd(new_balance))
