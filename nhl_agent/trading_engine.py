"""Paper/live execution via py-clob-client for NHL markets.

Same pattern as nba_agent/trading_engine.py. Uses shared bankroll
via shared/bankroll.py for cross-sport exposure tracking.
"""

from __future__ import annotations

import functools
import json
import logging
import time
import urllib.request
from typing import Optional

from nhl_agent.config import NHLConfig
from nhl_agent.models import NHLEdgeResult, NHLPosition, NHLTrade, Confidence
from nba_agent.utils import utcnow
from shared.bankroll import (
    load_bankroll,
    save_bankroll,
    get_total_open_exposure,
    check_total_exposure_ok,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fee estimation (same as NBA)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=256)
def _get_fee_rate(token_id: str) -> float:
    try:
        url = f"https://clob.polymarket.com/fee-rate?token_id={token_id}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return float(data.get("base_fee", 0))
    except Exception:
        return 0.0


def _polymarket_taker_fee(shares: float, price: float, token_id: str = "") -> float:
    if price <= 0 or price >= 1 or shares <= 0:
        return 0.0
    fee_rate = _get_fee_rate(token_id) if token_id else 0.0
    if fee_rate <= 0:
        return 0.0
    fee = shares * price * fee_rate * (price * (1 - price))
    return round(fee, 4)


# ---------------------------------------------------------------------------
# Bankroll manager (NHL-specific, uses shared bankroll)
# ---------------------------------------------------------------------------

class NHLBankrollManager:
    """NHL bankroll manager using shared bankroll with cross-sport exposure."""

    def __init__(self, config: NHLConfig | None = None) -> None:
        self.config = config or NHLConfig()
        state = load_bankroll()
        self.starting_bankroll = float(state.get("starting_bankroll", self.config.STARTING_BANKROLL))
        self.current_bankroll = float(state.get("current_bankroll", self.starting_bankroll))
        self.peak_bankroll = float(state.get("peak_bankroll", self.starting_bankroll))
        self.is_paused = bool(state.get("is_paused", False))
        self.is_reduced = bool(state.get("is_reduced", False))

    def reload(self) -> None:
        """Reload bankroll state from disk."""
        state = load_bankroll()
        self.current_bankroll = float(state.get("current_bankroll", self.starting_bankroll))
        self.peak_bankroll = float(state.get("peak_bankroll", self.starting_bankroll))
        self.is_paused = bool(state.get("is_paused", False))
        self.is_reduced = bool(state.get("is_reduced", False))

    def save_state(self) -> None:
        save_bankroll({
            "starting_bankroll": self.starting_bankroll,
            "current_bankroll": self.current_bankroll,
            "peak_bankroll": self.peak_bankroll,
            "is_paused": self.is_paused,
            "is_reduced": self.is_reduced,
        })

    def calculate_bet_size(self, edge_result: NHLEdgeResult) -> float:
        """Calculate bet size using half-Kelly, capped at 8%."""
        self.reload()  # Always read fresh bankroll

        if self.is_paused:
            return 0.0

        edge = edge_result.edge
        market_price = edge_result.market_price

        if market_price <= 0 or market_price >= 1:
            return 0.0

        odds_against = (1.0 - market_price) / market_price
        if odds_against <= 0:
            return 0.0

        kelly_fraction = (edge / odds_against) * 0.50  # Half Kelly
        bet_size = self.current_bankroll * kelly_fraction

        # HIGH confidence — conservative approach for new module
        if edge_result.confidence == Confidence.HIGH:
            bet_size *= 0.8  # Slightly reduce until we have data

        # Vegas agreement boost
        if edge_result.has_vegas_line and edge_result.vegas_agrees:
            bet_size *= 1.4
        elif edge_result.has_vegas_line and not edge_result.vegas_agrees:
            bet_size *= 0.7

        # Cap at 8% of bankroll
        max_bet = self.current_bankroll * self.config.MAX_BET_PCT
        bet_size = min(bet_size, max_bet)

        if self.is_reduced:
            bet_size *= 0.5

        # Floor at $1
        if bet_size < 1.0:
            return 0.0

        # Cross-sport exposure check
        if not check_total_exposure_ok(bet_size, self.config.MAX_TOTAL_EXPOSURE_PCT):
            logger.info("NHL bet blocked: would exceed total exposure limit (NBA + NHL)")
            return 0.0

        return round(bet_size, 2)

    def calculate_futures_bet_size(self, edge_result: NHLEdgeResult) -> float:
        """Calculate bet size for futures markets — capped at 4% of bankroll."""
        self.reload()

        if self.is_paused:
            return 0.0

        edge = edge_result.edge
        market_price = edge_result.market_price

        if market_price <= 0 or market_price >= 1:
            return 0.0

        odds_against = (1.0 - market_price) / market_price
        if odds_against <= 0:
            return 0.0

        kelly_fraction = (edge / odds_against) * 0.50  # Half Kelly
        bet_size = self.current_bankroll * kelly_fraction

        # Vegas agreement boost (smaller for futures)
        if edge_result.has_vegas_line and edge_result.vegas_agrees:
            bet_size *= 1.2
        elif edge_result.has_vegas_line and not edge_result.vegas_agrees:
            bet_size *= 0.6

        # Cap at 4% of bankroll for futures (half the game market limit)
        max_bet = self.current_bankroll * self.config.MAX_FUTURES_BET_PCT
        bet_size = min(bet_size, max_bet)

        if self.is_reduced:
            bet_size *= 0.5

        # Floor at $1
        if bet_size < 1.0:
            return 0.0

        # Cross-sport exposure check
        if not check_total_exposure_ok(bet_size, self.config.MAX_TOTAL_EXPOSURE_PCT):
            logger.info("NHL futures bet blocked: would exceed total exposure limit")
            return 0.0

        return round(bet_size, 2)

    def check_game_exposure(
        self,
        game_slug: str,
        open_positions: list[NHLPosition],
        proposed_bet: float,
    ) -> bool:
        current_exposure = sum(
            p.cost for p in open_positions
            if p.status == "open" and game_slug in p.market_slug
        )
        max_game = self.current_bankroll * self.config.MAX_GAME_EXPOSURE_PCT
        return (current_exposure + proposed_bet) <= max_game

    def update_bankroll(self, amount: float) -> None:
        """Update bankroll after a trade settles."""
        self.reload()
        self.current_bankroll += amount
        if self.current_bankroll > self.peak_bankroll:
            self.peak_bankroll = self.current_bankroll
        self._check_stop_loss()
        self.save_state()

    def _check_stop_loss(self) -> None:
        if self.current_bankroll < self.peak_bankroll * 0.60:
            if not self.is_paused:
                logger.critical("STOP LOSS: Bankroll $%.2f below 60%% of peak $%.2f",
                                self.current_bankroll, self.peak_bankroll)
                self.is_paused = True
                self.is_reduced = False
            return
        if self.current_bankroll < self.starting_bankroll * 0.80:
            if not self.is_reduced:
                logger.warning("Bankroll $%.2f below 80%% of starting — reducing sizes",
                               self.current_bankroll)
                self.is_reduced = True
        else:
            self.is_reduced = False
        if self.is_paused and self.current_bankroll >= self.peak_bankroll * 0.60:
            logger.info("Bankroll recovered — resuming trading")
            self.is_paused = False


# ---------------------------------------------------------------------------
# Trading engine
# ---------------------------------------------------------------------------

class NHLTradingEngine:
    """Executes NHL trades in paper or live mode."""

    def __init__(self, config: NHLConfig | None = None) -> None:
        self.config = config or NHLConfig()
        self._live_client = None

    def _get_live_client(self):
        if self._live_client is not None:
            return self._live_client

        if not self.config.is_live:
            return None

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            client = ClobClient(
                self.config.CLOB_API_BASE,
                key=self.config.PRIVATE_KEY,
                chain_id=137,
                signature_type=1,  # POLY_PROXY
                funder=self.config.FUNDER_ADDRESS,
            )

            if (self.config.POLYMARKET_API_KEY
                    and self.config.POLYMARKET_API_SECRET
                    and self.config.POLYMARKET_API_PASSPHRASE):
                client.set_api_creds(ApiCreds(
                    api_key=self.config.POLYMARKET_API_KEY,
                    api_secret=self.config.POLYMARKET_API_SECRET,
                    api_passphrase=self.config.POLYMARKET_API_PASSPHRASE,
                ))
            else:
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)

            self._live_client = client
            logger.info("NHL: Initialized live CLOB client")
            return self._live_client
        except Exception as e:
            logger.error("Failed to init live CLOB client: %s", e)
            return None

    def execute_buy(self, edge_result: NHLEdgeResult, bet_size: float) -> tuple[NHLPosition | None, NHLTrade | None]:
        """Execute a buy order."""
        market = edge_result.market
        side_index = edge_result.side_index

        if side_index >= len(market.clob_token_ids):
            return None, None

        token_id = market.clob_token_ids[side_index]
        price = market.outcome_prices[side_index]
        outcome_name = market.outcomes[side_index] if side_index < len(market.outcomes) else "Unknown"

        if price <= 0:
            return None, None

        now_str = utcnow().isoformat()
        shares = bet_size / price
        entry_fee = _polymarket_taker_fee(shares, price, token_id)

        if self.config.is_paper:
            order_id = f"paper_{int(time.time())}"
            logger.info("NHL PAPER BUY: %s @ %.2f¢ | $%.2f | %s",
                        outcome_name, price * 100, bet_size, market.question)
        else:
            order_id = self._execute_live_buy(token_id, bet_size, market.neg_risk)
            if not order_id:
                return None, None

        # Hours before faceoff
        hours_before = None
        if market.game_start_time:
            try:
                from datetime import datetime as _dt, timezone as _tz
                now_dt = _dt.now(_tz.utc)
                game_str = market.game_start_time.replace("Z", "+00:00")
                game_dt = _dt.fromisoformat(game_str)
                if game_dt.tzinfo is None:
                    game_dt = game_dt.replace(tzinfo=_tz.utc)
                hours_before = round((game_dt - now_dt).total_seconds() / 3600, 1)
            except Exception:
                pass

        # Opponent win pct
        opponent_win_pct = None
        if edge_result.research:
            try:
                our_team = outcome_name.lower()
                home = edge_result.research.home_team
                away = edge_result.research.away_team
                home_name_last = home.team_name.split()[-1].lower()
                if our_team.endswith(home_name_last) or home_name_last in our_team:
                    opponent_win_pct = round(away.win_pct, 3)
                else:
                    opponent_win_pct = round(home.win_pct, 3)
            except Exception:
                pass

        pos_id = f"nhl_pos_{int(time.time())}"
        position = NHLPosition(
            id=pos_id,
            market_id=market.id,
            market_question=market.question,
            token_id=token_id,
            side=f"YES ({outcome_name})",
            entry_price=price,
            shares=round(shares, 4),
            cost=bet_size,
            entry_time=now_str,
            confidence=edge_result.confidence.value,
            edge_at_entry=edge_result.edge,
            our_fair_price=edge_result.our_fair_price,
            mode="paper" if self.config.is_paper else "live",
            status="open",
            game_start_time=market.game_start_time,
            market_end_date=market.end_date,
            market_slug=market.slug,
            fees_paid=entry_fee,
            hours_before_faceoff=hours_before,
            opponent_win_pct=opponent_win_pct,
        )

        trade = NHLTrade(
            id=f"nhl_trade_{int(time.time())}",
            position_id=pos_id,
            market_id=market.id,
            market_question=market.question,
            action="BUY",
            side=f"YES ({outcome_name})",
            price=price,
            shares=round(shares, 4),
            amount=bet_size,
            timestamp=now_str,
            mode="paper" if self.config.is_paper else "live",
            order_id=order_id,
        )

        return position, trade

    def execute_sell(self, position: NHLPosition, current_price: float, reason: str) -> NHLTrade | None:
        """Execute a sell order to close a position."""
        now_str = utcnow().isoformat()

        if self.config.is_paper:
            order_id = f"paper_{int(time.time())}"
        else:
            order_id = self._execute_live_sell(position.token_id, position.shares, position.market_id)
            if not order_id:
                return None

        exit_value = position.shares * current_price
        exit_fee = _polymarket_taker_fee(position.shares, current_price, position.token_id)
        total_fees = position.fees_paid + exit_fee
        pnl = exit_value - position.cost - total_fees

        position.status = "closed"
        position.exit_price = current_price
        position.exit_time = now_str
        position.pnl = round(pnl, 2)
        position.exit_reason = reason
        position.fees_paid = round(total_fees, 4)

        return NHLTrade(
            id=f"nhl_trade_{int(time.time())}",
            position_id=position.id,
            market_id=position.market_id,
            market_question=position.market_question,
            action="SELL",
            side=position.side,
            price=current_price,
            shares=position.shares,
            amount=round(exit_value, 2),
            timestamp=now_str,
            mode=position.mode,
            order_id=order_id,
            pnl=round(pnl, 2),
        )

    def _execute_live_buy(self, token_id: str, amount: float, neg_risk: bool) -> str:
        """Execute a live buy order (limit then market fallback)."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client.order_builder.constants import BUY

            client = self._get_live_client()
            if not client:
                return ""

            tick_size = str(client.get_tick_size(token_id))
            mid_data = client.get_midpoint(token_id)
            midpoint = float(mid_data.get("mid", 0)) if isinstance(mid_data, dict) else float(mid_data)

            if midpoint <= 0:
                return self._execute_market_buy(token_id, amount, neg_risk)

            limit_price = round(midpoint, len(tick_size.split('.')[-1]) if '.' in tick_size else 2)
            shares = amount / limit_price if limit_price > 0 else 0
            if shares <= 0:
                return ""

            order_args = OrderArgs(token_id=token_id, price=limit_price, size=round(shares, 2), side=BUY)
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk if neg_risk else None)
            signed = client.create_order(order_args, options)
            resp = client.post_order(signed, OrderType.GTC)
            order_id = resp.get("orderID", "") if isinstance(resp, dict) else str(resp)

            if order_id:
                for _ in range(6):
                    time.sleep(5)
                    try:
                        info = client.get_order(order_id)
                        if isinstance(info, dict):
                            status = info.get("status", "").upper()
                            if status in ("MATCHED", "FILLED"):
                                return order_id
                            elif status in ("CANCELLED", "EXPIRED"):
                                break
                    except Exception:
                        pass
                try:
                    client.cancel_orders([order_id])
                except Exception:
                    pass

            return self._execute_market_buy(token_id, amount, neg_risk)

        except Exception as e:
            logger.error("NHL live BUY failed: %s", e)
            return ""

    def _execute_market_buy(self, token_id: str, amount: float, neg_risk: bool) -> str:
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            client = self._get_live_client()
            if not client:
                return ""

            mo = MarketOrderArgs(token_id=token_id, amount=amount, side=BUY, order_type=OrderType.FOK)
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, OrderType.FOK)
            order_id = resp.get("orderID", "") if isinstance(resp, dict) else str(resp)
            return order_id
        except Exception as e:
            logger.error("NHL MARKET BUY failed: %s", e)
            return ""

    def _execute_live_sell(self, token_id: str, shares: float, market_id: str) -> str:
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL

            client = self._get_live_client()
            if not client:
                return ""

            mo = MarketOrderArgs(token_id=token_id, amount=shares, side=SELL, order_type=OrderType.FOK)
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, OrderType.FOK)
            order_id = resp.get("orderID", "") if isinstance(resp, dict) else str(resp)
            return order_id
        except Exception as e:
            logger.error("NHL live SELL failed: %s", e)
            return ""
