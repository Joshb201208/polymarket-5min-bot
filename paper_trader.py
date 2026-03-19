"""
paper_trader.py - Paper trading simulation.

Simulates order execution without touching the Polymarket trading API.
Uses real Polymarket orderbook data for realistic fill simulation and
tracks a virtual balance.

Key design decisions:
  - Fill simulation based on actual ask/bid prices from CLOB
  - Resolution uses the real Chainlink oracle result (queried via Gamma API)
  - All trade data recorded exactly as in live mode (for comparison)
  - State persisted to JSON for session continuity
"""

import json
import time
import logging
import threading
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional
from datetime import datetime, timezone

import httpx

from utils import polymarket_fee, round_to_tick, format_usd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class SimulatedOrder:
    """A pending or filled simulated order."""
    order_id: str
    market_slug: str
    asset: str
    direction: str           # "YES" or "NO"
    token_id: str
    size_usd: float          # USDC we spend
    entry_price: float       # price we paid per share
    shares: float            # shares bought
    fee_paid: float
    placed_at: float         # Unix timestamp
    window_start: int
    window_end: int
    strategy: str
    edge: float
    confidence: float
    status: str = "pending"  # "pending" | "filled" | "resolved"


@dataclass
class SimulatedResult:
    """Result of a simulate_order() call."""
    success: bool
    order_id: str = ""
    filled_price: float = 0.0
    filled_size: float = 0.0        # shares
    size_usd: float = 0.0
    fee_paid: float = 0.0
    error: str = ""


@dataclass
class ResolvedTrade:
    """A paper trade that has been resolved with final P&L."""
    order_id: str
    market_slug: str
    asset: str
    direction: str
    size_usd: float
    entry_price: float
    exit_price: float        # 1.0 if correct, 0.0 if wrong
    shares: float
    gross_pnl: float
    fees_paid: float
    net_pnl: float
    balance_after: float
    won: bool
    window_start: int
    window_end: int
    strategy: str
    edge: float
    confidence: float
    resolved_at: float


# ---------------------------------------------------------------------------
# PaperTrader
# ---------------------------------------------------------------------------

class PaperTrader:
    """
    Simulates Polymarket trading without placing real orders.

    Fetches real orderbook data to simulate fills and uses market resolution
    data to compute P&L.
    """

    GAMMA_BASE = "https://gamma-api.polymarket.com"
    CLOB_BASE = "https://clob.polymarket.com"

    def __init__(
        self,
        initial_balance: float = 500.0,
        state_file: str = "paper_state.json",
    ):
        self._initial_balance = initial_balance
        self._balance = initial_balance
        self._state_file = state_file
        self._lock = threading.Lock()

        # Orders awaiting resolution
        self._open_orders: Dict[str, SimulatedOrder] = {}

        # Resolved trade history
        self._resolved: List[ResolvedTrade] = []

        # Counter for order IDs
        self._order_counter = 0

        # Load persisted state
        self._load_state()

        logger.info(
            "PaperTrader initialised: balance=%s, open_orders=%d",
            format_usd(self._balance),
            len(self._open_orders),
        )

    # ------------------------------------------------------------------
    # Order Simulation
    # ------------------------------------------------------------------

    def simulate_order(
        self,
        market: dict,
        side: str,
        size_usd: float,
        price: float,
        strategy: str = "unknown",
        edge: float = 0.0,
        confidence: float = 0.0,
    ) -> SimulatedResult:
        """
        Simulate placing an order.

        Uses real orderbook data from CLOB to determine whether the order
        would have been filled and at what price.

        Args:
            market:      Market dict from MarketFinder.
            side:        "YES" (Up) or "NO" (Down).
            size_usd:    USDC amount to spend.
            price:       Desired limit price.
            strategy:    Name of the strategy generating the signal.
            edge:        Signal edge.
            confidence:  Signal confidence.

        Returns:
            SimulatedResult with fill details.
        """
        if size_usd <= 0:
            return SimulatedResult(success=False, error="Invalid size")

        if size_usd > self._balance:
            return SimulatedResult(
                success=False,
                error=f"Insufficient balance: {format_usd(self._balance)} < {format_usd(size_usd)}",
            )

        token_id = market.get("token_id_yes") if side == "YES" else market.get("token_id_no")
        if not token_id:
            return SimulatedResult(
                success=False,
                error=f"No token_id for {side} in market {market.get('slug')}",
            )

        # Fetch real orderbook to get fill price
        fill_price = self._get_fill_price(token_id, price)
        if fill_price is None:
            # Market data unavailable — use the passed price as fallback
            fill_price = price
            logger.debug("PaperTrader: using fallback price %.3f for %s", fill_price, token_id[:16])

        # Check if order would fill: FOK semantics
        # We consider filled if fill_price <= limit price (we're buying)
        if fill_price > price + 0.02:  # allow 2 tick slippage
            return SimulatedResult(
                success=False,
                error=f"Orderbook price {fill_price:.3f} > limit {price:.3f} — would not fill",
            )

        # Calculate fee
        fee = polymarket_fee(shares=size_usd / fill_price, price=fill_price)
        actual_cost = size_usd + fee
        shares = size_usd / fill_price

        with self._lock:
            if actual_cost > self._balance:
                return SimulatedResult(
                    success=False,
                    error=f"Cost+fee {format_usd(actual_cost)} exceeds balance {format_usd(self._balance)}",
                )

            # Deduct cost from paper balance
            self._balance -= actual_cost

            self._order_counter += 1
            order_id = f"PAPER-{self._order_counter:06d}-{int(time.time())}"

            order = SimulatedOrder(
                order_id=order_id,
                market_slug=market.get("slug", ""),
                asset=market.get("asset", ""),
                direction=side,
                token_id=token_id,
                size_usd=size_usd,
                entry_price=fill_price,
                shares=shares,
                fee_paid=fee,
                placed_at=time.time(),
                window_start=market.get("window_start", 0),
                window_end=market.get("window_end", 0),
                strategy=strategy,
                edge=edge,
                confidence=confidence,
                status="filled",
            )
            self._open_orders[order_id] = order

        self._save_state()

        logger.info(
            "PaperTrader: simulated fill %s | %s %s at %.3f | shares=%.2f fee=%s | bal=%s",
            order_id, side, market.get("asset"), fill_price,
            shares, format_usd(fee), format_usd(self._balance),
        )

        return SimulatedResult(
            success=True,
            order_id=order_id,
            filled_price=fill_price,
            filled_size=shares,
            size_usd=size_usd,
            fee_paid=fee,
        )

    # ------------------------------------------------------------------
    # Position Resolution
    # ------------------------------------------------------------------

    def resolve_positions(self) -> List[ResolvedTrade]:
        """
        Check all open simulated orders whose window has ended and resolve them.

        For each resolved market, queries the Gamma API to get the final
        outcome (Up or Down) and computes P&L.

        Returns:
            List of newly resolved trades.
        """
        now = time.time()
        newly_resolved: List[ResolvedTrade] = []

        with self._lock:
            order_ids = list(self._open_orders.keys())

        for order_id in order_ids:
            with self._lock:
                order = self._open_orders.get(order_id)
            if order is None:
                continue

            # Only resolve after the window has ended (plus a small buffer)
            if now < order.window_end + 10:
                continue

            # Query Gamma to find the resolution result
            outcome = self._query_resolution(order.market_slug)

            if outcome is None:
                # Not yet resolved — check again next iteration
                if now > order.window_end + 300:
                    # Give up after 5 minutes
                    logger.warning(
                        "PaperTrader: could not resolve %s after 5 min, marking as failed",
                        order.market_slug,
                    )
                    outcome = "unknown"
                else:
                    continue

            # Determine if our bet was correct
            # direction="YES" means we bet on "Up"; direction="NO" means we bet on "Down"
            won = self._determine_win(order.direction, outcome)

            # P&L calculation
            if won:
                # Each share pays $1.00 on resolution
                exit_price = 1.0
                gross_pnl = order.shares * (exit_price - order.entry_price)
            else:
                exit_price = 0.0
                gross_pnl = -order.entry_price * order.shares

            net_pnl = gross_pnl - order.fee_paid

            with self._lock:
                self._balance += order.shares * exit_price  # return proceeds
                balance_after = self._balance
                del self._open_orders[order_id]

                resolved = ResolvedTrade(
                    order_id=order_id,
                    market_slug=order.market_slug,
                    asset=order.asset,
                    direction=order.direction,
                    size_usd=order.size_usd,
                    entry_price=order.entry_price,
                    exit_price=exit_price,
                    shares=order.shares,
                    gross_pnl=gross_pnl,
                    fees_paid=order.fee_paid,
                    net_pnl=net_pnl,
                    balance_after=balance_after,
                    won=won,
                    window_start=order.window_start,
                    window_end=order.window_end,
                    strategy=order.strategy,
                    edge=order.edge,
                    confidence=order.confidence,
                    resolved_at=time.time(),
                )
                self._resolved.append(resolved)
                newly_resolved.append(resolved)

            result_str = "WIN" if won else "LOSS"
            logger.info(
                "PaperTrader resolved: %s | %s %s outcome=%s %s pnl=%s bal=%s",
                order_id, order.asset, order.direction,
                outcome, result_str,
                format_usd(net_pnl), format_usd(balance_after),
            )

        if newly_resolved:
            self._save_state()

        return newly_resolved

    def _determine_win(self, direction: str, outcome: str) -> bool:
        """
        Determine if our bet won given the market outcome.

        direction: "YES" = bet on Up, "NO" = bet on Down
        outcome:   "up", "down", or "unknown"
        """
        outcome_lower = outcome.lower()
        if outcome_lower == "unknown":
            return False  # conservative: mark as loss if unresolvable
        if direction.upper() == "YES":
            return outcome_lower in ("up", "yes", "higher")
        else:  # "NO" = Down bet
            return outcome_lower in ("down", "no", "lower")

    # ------------------------------------------------------------------
    # Resolution Query
    # ------------------------------------------------------------------

    def _query_resolution(self, slug: str) -> Optional[str]:
        """
        Query Gamma API for the resolved outcome of a market.

        Returns:
            "up", "down", or None if not yet resolved.
        """
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(f"{self.GAMMA_BASE}/markets/slug/{slug}")
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()

            if not data.get("closed", False) and not data.get("resolved", False):
                return None

            # Try to determine the winning outcome
            # Gamma stores resolution in different fields depending on version
            resolution = data.get("resolution", "")
            winner = data.get("winner", "")
            tokens = data.get("tokens", [])

            # Check resolved prices: winning token = 1.0
            for token in tokens:
                if isinstance(token, dict):
                    outcome = str(token.get("outcome", "")).lower()
                    price = float(token.get("price", 0) or 0)
                    if price >= 0.99:  # resolved as winner
                        return outcome

            # Fall back to resolution string
            combined = (resolution + winner).lower()
            if "up" in combined or "higher" in combined or "yes" in combined:
                return "up"
            if "down" in combined or "lower" in combined or "no" in combined:
                return "down"

            return None

        except Exception as exc:
            logger.debug("PaperTrader: resolution query failed for %s: %s", slug, exc)
            return None

    # ------------------------------------------------------------------
    # Price Fetching
    # ------------------------------------------------------------------

    def _get_fill_price(self, token_id: str, desired_price: float) -> Optional[float]:
        """
        Get the current best ask price from the CLOB orderbook.

        Returns:
            Best ask price, or None on error.
        """
        try:
            with httpx.Client(timeout=6) as client:
                resp = client.get(
                    f"{self.CLOB_BASE}/book",
                    params={"token_id": token_id},
                )
                resp.raise_for_status()
                book = resp.json()

            asks = book.get("asks", [])
            if asks:
                best_ask = min(float(a.get("price", 1.0)) for a in asks)
                return round_to_tick(best_ask)

            # Fallback: use midpoint
            resp2 = httpx.get(
                f"{self.CLOB_BASE}/midpoint",
                params={"token_id": token_id},
                timeout=6,
            )
            resp2.raise_for_status()
            mid = float(resp2.json().get("mid", 0.5))
            return round_to_tick(mid + 0.01)  # add 1 tick for buy slippage

        except Exception as exc:
            logger.debug("PaperTrader: fill price error for %s: %s", token_id[:16], exc)
            return None

    # ------------------------------------------------------------------
    # State Persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Save paper trading state to JSON file."""
        try:
            with self._lock:
                state = {
                    "balance": self._balance,
                    "order_counter": self._order_counter,
                    "open_orders": {
                        k: asdict(v) for k, v in self._open_orders.items()
                    },
                    "resolved_count": len(self._resolved),
                    "updated": datetime.now(tz=timezone.utc).isoformat(),
                }
            with open(self._state_file, "w") as f:
                json.dump(state, f, indent=2, default=str)
        except Exception as exc:
            logger.warning("PaperTrader: could not save state: %s", exc)

    def _load_state(self) -> None:
        """Load persisted paper trading state."""
        import os
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file) as f:
                state = json.load(f)

            with self._lock:
                self._balance = float(state.get("balance", self._initial_balance))
                self._order_counter = int(state.get("order_counter", 0))

                for order_id, od in state.get("open_orders", {}).items():
                    try:
                        self._open_orders[order_id] = SimulatedOrder(**od)
                    except Exception:
                        pass

            logger.info(
                "PaperTrader: loaded state — balance=%s, open_orders=%d",
                format_usd(self._balance),
                len(self._open_orders),
            )
        except Exception as exc:
            logger.warning("PaperTrader: could not load state: %s", exc)

    # ------------------------------------------------------------------
    # Public Accessors
    # ------------------------------------------------------------------

    def get_balance(self) -> float:
        """Return current paper balance."""
        with self._lock:
            return self._balance

    def get_open_orders(self) -> List[SimulatedOrder]:
        """Return list of open (unresolved) simulated orders."""
        with self._lock:
            return list(self._open_orders.values())

    def get_resolved_trades(self) -> List[ResolvedTrade]:
        """Return all resolved trade records."""
        with self._lock:
            return list(self._resolved)

    def get_stats(self) -> dict:
        """Quick stats summary."""
        with self._lock:
            resolved = list(self._resolved)
            balance = self._balance

        total = len(resolved)
        if total == 0:
            return {
                "total_trades": 0,
                "balance": balance,
                "pnl": balance - self._initial_balance,
            }

        wins = sum(1 for t in resolved if t.won)
        pnl = sum(t.net_pnl for t in resolved)
        return {
            "total_trades": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": wins / total,
            "total_pnl": pnl,
            "balance": balance,
            "return_pct": (balance - self._initial_balance) / self._initial_balance,
        }
