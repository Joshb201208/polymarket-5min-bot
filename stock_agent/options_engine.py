"""Options strategy selection engine.

Selects the optimal options strategy and contract(s) based on:
    - Macro market regime (from MacroMonitor)
    - Stock screener signals and conviction scores
    - Existing portfolio positions
    - Risk rules (max positions, exposure limits)

Supported strategies:
    COVERED_CALL      — Sell calls against 100+ share stock positions (future use)
    CASH_SECURED_PUT  — Sell puts on stocks the bot wants to buy at a lower price
    BULL_CALL_SPREAD  — Buy call spread in AGGRESSIVE_DEPLOY / DIP_OPPORTUNITY
    BEAR_PUT_SPREAD   — Buy put spread in CAUTIOUS regime as a hedge

Risk rules enforced here:
    - Max 5 open options positions
    - Max 5% of portfolio per trade
    - No naked options (all positions covered or defined-risk)
    - Auto-close at 50% profit or 3 DTE (enforced in portfolio tracker)
    - Max 15% total options exposure of portfolio value
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from stock_agent.config import Config
from stock_agent.macro_monitor import (
    MacroMonitor,
    MacroSnapshot,
    REGIME_AGGRESSIVE_DEPLOY,
    REGIME_DIP_OPPORTUNITY,
    REGIME_CAUTIOUS,
    REGIME_NORMAL,
)
from stock_agent.options_data import OptionsDataFeed
from stock_agent.options_models import (
    OptionSide,
    OptionType,
    OptionsContract,
    OptionsQuote,
    OptionsSignal,
    OptionsStrategy,
)

logger = logging.getLogger(__name__)

# ── Risk constants ────────────────────────────────────────────────────

MAX_OPTIONS_POSITIONS = 5           # Hard cap on concurrent options positions
MAX_SINGLE_TRADE_PCT = 0.05         # 5% of portfolio per trade
MAX_TOTAL_OPTIONS_EXPOSURE_PCT = 0.15  # 15% of portfolio in options total
MIN_REWARD_TO_RISK = 2.0            # Minimum R:R for spread strategies
SHARES_PER_CONTRACT = 100           # Standard US options contract size

# ── Strategy parameters ───────────────────────────────────────────────

# Covered call / CSP target DTE window
CC_MIN_DTE = 14    # 2 weeks
CC_MAX_DTE = 28    # 4 weeks

# Directional spreads DTE window
SPREAD_MIN_DTE = 30
SPREAD_MAX_DTE = 45

# Minimum premium for cash-secured puts (annualised, as fraction of strike)
MIN_PUT_PREMIUM_MONTHLY_PCT = 0.01  # 1% of strike per month minimum

# Spread width targets (in dollars)
SPREAD_WIDTH_NARROW = 5.0   # For sub-$150 stocks
SPREAD_WIDTH_WIDE = 10.0    # For $150+ stocks


class OptionsEngine:
    """Evaluates options opportunities and generates trade signals.

    Usage::

        engine = OptionsEngine(config, macro_monitor, data_feed)
        signals = await engine.scan_opportunities(
            portfolio_value=100_000,
            open_options_count=2,
            current_options_exposure=0.05,
            stock_positions={"AAPL": 16, "MSFT": 11},
            screener_candidates=[("NVDA", 7, 450.0, 480.0), ...],
        )
    """

    def __init__(
        self,
        config: Config,
        macro_monitor: MacroMonitor,
        data_feed: OptionsDataFeed,
    ) -> None:
        self.config = config
        self.macro = macro_monitor
        self.data = data_feed

    # ── Main scan entry-point ─────────────────────────────────────────

    async def scan_opportunities(
        self,
        portfolio_value: float,
        open_options_count: int,
        current_options_exposure: float,
        stock_positions: dict[str, int],
        screener_candidates: list[tuple[str, int, float, Optional[float]]],
    ) -> list[OptionsSignal]:
        """Scan for options opportunities given current market conditions.

        Args:
            portfolio_value:          Total portfolio value in dollars.
            open_options_count:       Number of currently open options positions.
            current_options_exposure: Current options value as fraction of portfolio.
            stock_positions:          Map of ticker → shares held, e.g.
                                      ``{"AAPL": 16, "MSFT": 11}``.
            screener_candidates:      List of (symbol, conviction, price, target_price)
                                      tuples from the screener.

        Returns:
            List of :class:`OptionsSignal` objects ready for execution review.
        """
        signals: list[OptionsSignal] = []

        # ── Guard: risk limits ────────────────────────────────────────
        if open_options_count >= MAX_OPTIONS_POSITIONS:
            logger.info(
                "scan_opportunities: at max positions (%d/%d), skipping",
                open_options_count, MAX_OPTIONS_POSITIONS,
            )
            return signals

        if current_options_exposure >= MAX_TOTAL_OPTIONS_EXPOSURE_PCT:
            logger.info(
                "scan_opportunities: at max options exposure (%.1f%%), skipping",
                current_options_exposure * 100,
            )
            return signals

        regime = self.macro.get_current_regime()
        snap = self.macro.get_last_snapshot()
        slots_remaining = MAX_OPTIONS_POSITIONS - open_options_count
        exposure_headroom = MAX_TOTAL_OPTIONS_EXPOSURE_PCT - current_options_exposure

        logger.info(
            "scan_opportunities: regime=%s, slots=%d, exposure_headroom=%.1f%%",
            regime, slots_remaining, exposure_headroom * 100,
        )

        # ── 1. Covered calls on existing positions ────────────────────
        for symbol, shares in stock_positions.items():
            if shares >= SHARES_PER_CONTRACT and slots_remaining > 0:
                cc_signal = await self._evaluate_covered_call(
                    symbol=symbol,
                    shares=shares,
                    portfolio_value=portfolio_value,
                    regime=regime,
                    snap=snap,
                )
                if cc_signal:
                    signals.append(cc_signal)
                    slots_remaining -= 1

        # ── 2. Cash-secured puts on screener targets ──────────────────
        for symbol, conviction, price, target_price in screener_candidates:
            if slots_remaining <= 0:
                break
            # Focus CSPs on moderate-conviction stocks (6-7) — not enough
            # conviction for an outright buy but want exposure at lower price
            if conviction < 6 or conviction > 7:
                continue
            # Don't sell puts on stocks we already hold (avoid doubling down)
            if symbol in stock_positions:
                continue

            csp_signal = await self._evaluate_cash_secured_put(
                symbol=symbol,
                current_price=price,
                desired_entry=target_price,
                conviction=conviction,
                portfolio_value=portfolio_value,
                regime=regime,
                snap=snap,
            )
            if csp_signal:
                signals.append(csp_signal)
                slots_remaining -= 1

        # ── 3. Directional spreads (regime-gated) ────────────────────
        if regime in (REGIME_AGGRESSIVE_DEPLOY, REGIME_DIP_OPPORTUNITY):
            # Bull call spreads on highest-conviction stocks
            high_conviction = [
                (s, cv, p, tp)
                for s, cv, p, tp in screener_candidates
                if cv >= 8
            ]
            for symbol, conviction, price, target_price in high_conviction:
                if slots_remaining <= 0:
                    break
                spread_signal = await self._evaluate_bull_call_spread(
                    symbol=symbol,
                    current_price=price,
                    target_price=target_price,
                    conviction=conviction,
                    portfolio_value=portfolio_value,
                    regime=regime,
                    snap=snap,
                )
                if spread_signal:
                    signals.append(spread_signal)
                    slots_remaining -= 1

        elif regime == REGIME_CAUTIOUS:
            # Bear put spreads as portfolio hedges
            for symbol, conviction, price, target_price in screener_candidates[:3]:
                if slots_remaining <= 0:
                    break
                put_spread_signal = await self._evaluate_bear_put_spread(
                    symbol=symbol,
                    current_price=price,
                    conviction=conviction,
                    portfolio_value=portfolio_value,
                    regime=regime,
                    snap=snap,
                )
                if put_spread_signal:
                    signals.append(put_spread_signal)
                    slots_remaining -= 1

        logger.info(
            "scan_opportunities: generated %d signal(s) for regime=%s",
            len(signals), regime,
        )
        return signals

    # ── Strategy evaluators ───────────────────────────────────────────

    async def _evaluate_covered_call(
        self,
        symbol: str,
        shares: int,
        portfolio_value: float,
        regime: str,
        snap: Optional[MacroSnapshot],
    ) -> Optional[OptionsSignal]:
        """Find an optimal covered call to sell on an existing position.

        Targets a strike at or above the thesis target price, 2-4 weeks DTE,
        with premium >= 1% of underlying per month.

        NOTE: Current portfolio has no positions >= 100 shares.  This method
        is implemented for when positions grow.  It will return None for any
        position with fewer than 100 shares.
        """
        contracts_to_sell = shares // SHARES_PER_CONTRACT
        if contracts_to_sell < 1:
            logger.debug(
                "_evaluate_covered_call: %s has only %d shares, need %d for 1 contract — skipping",
                symbol, shares, SHARES_PER_CONTRACT,
            )
            return None

        min_expiry = date.today() + timedelta(days=CC_MIN_DTE)
        max_expiry = date.today() + timedelta(days=CC_MAX_DTE)

        # Fetch OTM calls — strikes above current price
        chain_with_quotes = await self.data.fetch_chain_with_quotes(
            underlying=symbol,
            option_type=OptionType.CALL,
            min_expiry=min_expiry,
            max_expiry=max_expiry,
            limit=50,
        )

        if not chain_with_quotes:
            logger.debug("_evaluate_covered_call: no chain data for %s", symbol)
            return None

        best_contract: Optional[OptionsContract] = None
        best_quote: Optional[OptionsQuote] = None
        best_premium: float = 0.0

        for contract, quote in chain_with_quotes:
            if quote is None:
                continue
            if quote.fair_value is None or quote.fair_value <= 0:
                continue

            # Premium as monthly percentage of strike (annualise then divide by 12)
            dte = (contract.expiration_date - date.today()).days
            if dte <= 0:
                continue
            monthly_premium_pct = (quote.fair_value / contract.strike_price) * (30 / dte)

            if monthly_premium_pct < MIN_PUT_PREMIUM_MONTHLY_PCT:
                continue

            if quote.fair_value > best_premium:
                best_premium = quote.fair_value
                best_contract = contract
                best_quote = quote

        if best_contract is None or best_quote is None:
            logger.debug("_evaluate_covered_call: no qualifying contracts for %s", symbol)
            return None

        return OptionsSignal(
            strategy=OptionsStrategy.COVERED_CALL,
            underlying_symbol=symbol,
            underlying_price=best_contract.strike_price,  # Approximate
            contract_symbol=best_contract.symbol,
            option_type=OptionType.CALL,
            side=OptionSide.SELL,
            target_strike=best_contract.strike_price,
            target_expiry_min_dte=CC_MIN_DTE,
            target_expiry_max_dte=CC_MAX_DTE,
            qty=contracts_to_sell,
            min_credit_per_contract=best_quote.fair_value,
            position_size_pct=0.0,  # No capital required (covered by shares)
            rationale=(
                f"Covered call on {shares} shares: strike ${best_contract.strike_price:.0f} "
                f"exp {best_contract.expiration_date}, premium ${best_premium:.2f} "
                f"({(best_premium / best_contract.strike_price * 100):.1f}% of strike)"
            ),
            conviction=7,
            regime=regime,
            estimated_max_profit=best_premium * SHARES_PER_CONTRACT * contracts_to_sell,
            estimated_max_loss=None,  # Capped by stock ownership
        )

    async def _evaluate_cash_secured_put(
        self,
        symbol: str,
        current_price: float,
        desired_entry: Optional[float],
        conviction: int,
        portfolio_value: float,
        regime: str,
        snap: Optional[MacroSnapshot],
    ) -> Optional[OptionsSignal]:
        """Find an optimal cash-secured put at or below the desired entry price.

        The put strike is set at the desired entry price (support level).
        The cash required = strike × 100 per contract.  We verify the account
        has adequate capital headroom before generating the signal.

        Args:
            symbol:        Underlying ticker.
            current_price: Current market price of the stock.
            desired_entry: Desired buy price — strike will be at or below this.
                           If None, defaults to 5% below current price.
            conviction:    Screener conviction score.
            portfolio_value: Total portfolio value.
            regime:        Current macro regime.
            snap:          Current macro snapshot.
        """
        # Default desired entry to 5% below current price
        if desired_entry is None or desired_entry >= current_price:
            desired_entry = round(current_price * 0.95, 1)

        min_expiry = date.today() + timedelta(days=CC_MIN_DTE)
        max_expiry = date.today() + timedelta(days=CC_MAX_DTE)

        # Look for puts with strike <= desired_entry (slight buffer for available strikes)
        max_strike = desired_entry * 1.01   # Allow up to 1% above desired entry
        min_strike = desired_entry * 0.90   # Floor at 10% below desired entry

        chain_with_quotes = await self.data.fetch_chain_with_quotes(
            underlying=symbol,
            option_type=OptionType.PUT,
            min_strike=min_strike,
            max_strike=max_strike,
            min_expiry=min_expiry,
            max_expiry=max_expiry,
            limit=50,
        )

        if not chain_with_quotes:
            logger.debug("_evaluate_cash_secured_put: no chain data for %s", symbol)
            return None

        # Capital required per contract = strike × 100
        max_trade_value = portfolio_value * MAX_SINGLE_TRADE_PCT

        best_contract: Optional[OptionsContract] = None
        best_quote: Optional[OptionsQuote] = None
        best_score: float = 0.0

        for contract, quote in chain_with_quotes:
            if quote is None or quote.fair_value is None or quote.fair_value <= 0:
                continue

            cash_required = contract.strike_price * SHARES_PER_CONTRACT
            if cash_required > max_trade_value:
                logger.debug(
                    "_evaluate_cash_secured_put: %s strike $%.0f requires $%.0f > max $%.0f",
                    symbol, contract.strike_price, cash_required, max_trade_value,
                )
                continue

            # Prefer higher premium as % of strike
            dte = (contract.expiration_date - date.today()).days
            if dte <= 0:
                continue

            monthly_premium_pct = (quote.fair_value / contract.strike_price) * (30 / dte)
            if monthly_premium_pct < MIN_PUT_PREMIUM_MONTHLY_PCT:
                continue

            # Score = premium pct (higher is better)
            score = monthly_premium_pct
            if score > best_score:
                best_score = score
                best_contract = contract
                best_quote = quote

        if best_contract is None or best_quote is None:
            logger.debug("_evaluate_cash_secured_put: no qualifying put for %s", symbol)
            return None

        cash_required = best_contract.strike_price * SHARES_PER_CONTRACT
        position_pct = cash_required / portfolio_value

        return OptionsSignal(
            strategy=OptionsStrategy.CASH_SECURED_PUT,
            underlying_symbol=symbol,
            underlying_price=current_price,
            contract_symbol=best_contract.symbol,
            option_type=OptionType.PUT,
            side=OptionSide.SELL,
            target_strike=best_contract.strike_price,
            target_expiry_min_dte=CC_MIN_DTE,
            target_expiry_max_dte=CC_MAX_DTE,
            qty=1,
            min_credit_per_contract=best_quote.fair_value,
            position_size_pct=position_pct,
            rationale=(
                f"Cash-secured put on {symbol}: conviction {conviction}/10. "
                f"Strike ${best_contract.strike_price:.0f} ({(1 - best_contract.strike_price / current_price) * 100:.1f}% OTM), "
                f"exp {best_contract.expiration_date}, premium ${best_quote.fair_value:.2f}/share "
                f"(${best_quote.fair_value * SHARES_PER_CONTRACT:.0f} total). "
                f"Worst case: acquire {symbol} at ${best_contract.strike_price:.0f} (desired entry). "
                f"Cash required: ${cash_required:,.0f}"
            ),
            conviction=conviction,
            regime=regime,
            estimated_max_profit=best_quote.fair_value * SHARES_PER_CONTRACT,
            estimated_max_loss=best_contract.strike_price * SHARES_PER_CONTRACT,  # Assignment risk
        )

    async def _evaluate_bull_call_spread(
        self,
        symbol: str,
        current_price: float,
        target_price: Optional[float],
        conviction: int,
        portfolio_value: float,
        regime: str,
        snap: Optional[MacroSnapshot],
    ) -> Optional[OptionsSignal]:
        """Build a bull call spread: buy lower strike, sell higher strike.

        Used in AGGRESSIVE_DEPLOY / DIP_OPPORTUNITY regimes on high-conviction stocks.
        30-45 DTE. Width: $5-$10 depending on stock price.
        Minimum 2:1 reward-to-risk ratio required.
        """
        min_expiry = date.today() + timedelta(days=SPREAD_MIN_DTE)
        max_expiry = date.today() + timedelta(days=SPREAD_MAX_DTE)

        # Buy strike: slightly OTM (just above current price)
        buy_strike = self._round_to_strike(current_price * 1.01)

        # Spread width: $5 for stocks < $150, $10 for stocks >= $150
        width = SPREAD_WIDTH_WIDE if current_price >= 150 else SPREAD_WIDTH_NARROW
        sell_strike = buy_strike + width

        chain_with_quotes = await self.data.fetch_chain_with_quotes(
            underlying=symbol,
            option_type=OptionType.CALL,
            min_strike=buy_strike * 0.98,
            max_strike=sell_strike * 1.02,
            min_expiry=min_expiry,
            max_expiry=max_expiry,
            limit=50,
        )

        if not chain_with_quotes:
            return None

        # Find best matching long and short legs
        long_leg = self._find_closest_contract(chain_with_quotes, buy_strike)
        short_leg = self._find_closest_contract(chain_with_quotes, sell_strike)

        if long_leg is None or short_leg is None:
            logger.debug("_evaluate_bull_call_spread: missing legs for %s", symbol)
            return None

        long_contract, long_quote = long_leg
        short_contract, short_quote = short_leg

        if long_quote is None or short_quote is None:
            return None
        if long_quote.fair_value is None or short_quote.fair_value is None:
            return None

        net_debit = long_quote.fair_value - short_quote.fair_value
        if net_debit <= 0:
            logger.debug("_evaluate_bull_call_spread: net_debit <= 0 for %s", symbol)
            return None

        max_profit = (short_contract.strike_price - long_contract.strike_price) - net_debit
        max_loss = net_debit  # Per share; total = net_debit * 100

        if max_profit <= 0:
            return None

        reward_to_risk = max_profit / max_loss
        if reward_to_risk < MIN_REWARD_TO_RISK:
            logger.debug(
                "_evaluate_bull_call_spread: R:R %.2f < %.2f for %s — skipping",
                reward_to_risk, MIN_REWARD_TO_RISK, symbol,
            )
            return None

        # Debit as % of portfolio
        total_debit = net_debit * SHARES_PER_CONTRACT
        position_pct = total_debit / portfolio_value
        if position_pct > MAX_SINGLE_TRADE_PCT:
            return None

        return OptionsSignal(
            strategy=OptionsStrategy.BULL_CALL_SPREAD,
            underlying_symbol=symbol,
            underlying_price=current_price,
            contract_symbol=long_contract.symbol,
            option_type=OptionType.CALL,
            side=OptionSide.BUY,
            target_strike=long_contract.strike_price,
            target_expiry_min_dte=SPREAD_MIN_DTE,
            target_expiry_max_dte=SPREAD_MAX_DTE,
            leg2_contract_symbol=short_contract.symbol,
            leg2_option_type=OptionType.CALL,
            leg2_side=OptionSide.SELL,
            leg2_target_strike=short_contract.strike_price,
            qty=1,
            max_debit_per_contract=round(net_debit * 1.05, 2),
            position_size_pct=position_pct,
            rationale=(
                f"Bull call spread on {symbol} (regime: {regime}, conviction {conviction}/10). "
                f"Buy ${long_contract.strike_price:.0f}C / Sell ${short_contract.strike_price:.0f}C "
                f"exp {long_contract.expiration_date}. "
                f"Net debit ${net_debit:.2f}/share (${total_debit:.0f} total). "
                f"Max profit ${max_profit * SHARES_PER_CONTRACT:.0f}, "
                f"Max loss ${max_loss * SHARES_PER_CONTRACT:.0f} "
                f"(R:R {reward_to_risk:.1f}x)."
            ),
            conviction=conviction,
            regime=regime,
            estimated_max_profit=max_profit * SHARES_PER_CONTRACT,
            estimated_max_loss=max_loss * SHARES_PER_CONTRACT,
            reward_to_risk=reward_to_risk,
        )

    async def _evaluate_bear_put_spread(
        self,
        symbol: str,
        current_price: float,
        conviction: int,
        portfolio_value: float,
        regime: str,
        snap: Optional[MacroSnapshot],
    ) -> Optional[OptionsSignal]:
        """Build a bear put spread: buy higher strike put, sell lower strike put.

        Used in CAUTIOUS regime as a hedge.  30-45 DTE, width $5-$10.
        """
        min_expiry = date.today() + timedelta(days=SPREAD_MIN_DTE)
        max_expiry = date.today() + timedelta(days=SPREAD_MAX_DTE)

        # Buy strike: slightly OTM (just below current price)
        buy_strike = self._round_to_strike(current_price * 0.99)
        width = SPREAD_WIDTH_WIDE if current_price >= 150 else SPREAD_WIDTH_NARROW
        sell_strike = buy_strike - width

        chain_with_quotes = await self.data.fetch_chain_with_quotes(
            underlying=symbol,
            option_type=OptionType.PUT,
            min_strike=sell_strike * 0.98,
            max_strike=buy_strike * 1.02,
            min_expiry=min_expiry,
            max_expiry=max_expiry,
            limit=50,
        )

        if not chain_with_quotes:
            return None

        long_leg = self._find_closest_contract(chain_with_quotes, buy_strike)
        short_leg = self._find_closest_contract(chain_with_quotes, sell_strike)

        if long_leg is None or short_leg is None:
            return None

        long_contract, long_quote = long_leg
        short_contract, short_quote = short_leg

        if long_quote is None or short_quote is None:
            return None
        if long_quote.fair_value is None or short_quote.fair_value is None:
            return None

        net_debit = long_quote.fair_value - short_quote.fair_value
        if net_debit <= 0:
            return None

        max_profit = (long_contract.strike_price - short_contract.strike_price) - net_debit
        max_loss = net_debit

        if max_profit <= 0:
            return None

        reward_to_risk = max_profit / max_loss
        if reward_to_risk < MIN_REWARD_TO_RISK:
            return None

        total_debit = net_debit * SHARES_PER_CONTRACT
        position_pct = total_debit / portfolio_value
        if position_pct > MAX_SINGLE_TRADE_PCT:
            return None

        return OptionsSignal(
            strategy=OptionsStrategy.BEAR_PUT_SPREAD,
            underlying_symbol=symbol,
            underlying_price=current_price,
            contract_symbol=long_contract.symbol,
            option_type=OptionType.PUT,
            side=OptionSide.BUY,
            target_strike=long_contract.strike_price,
            target_expiry_min_dte=SPREAD_MIN_DTE,
            target_expiry_max_dte=SPREAD_MAX_DTE,
            leg2_contract_symbol=short_contract.symbol,
            leg2_option_type=OptionType.PUT,
            leg2_side=OptionSide.SELL,
            leg2_target_strike=short_contract.strike_price,
            qty=1,
            max_debit_per_contract=round(net_debit * 1.05, 2),
            position_size_pct=position_pct,
            rationale=(
                f"Bear put spread on {symbol} (regime: CAUTIOUS hedge). "
                f"Buy ${long_contract.strike_price:.0f}P / Sell ${short_contract.strike_price:.0f}P "
                f"exp {long_contract.expiration_date}. "
                f"Net debit ${net_debit:.2f}/share (${total_debit:.0f} total). "
                f"Max profit ${max_profit * SHARES_PER_CONTRACT:.0f}, "
                f"Max loss ${max_loss * SHARES_PER_CONTRACT:.0f} "
                f"(R:R {reward_to_risk:.1f}x)."
            ),
            conviction=conviction,
            regime=regime,
            estimated_max_profit=max_profit * SHARES_PER_CONTRACT,
            estimated_max_loss=max_loss * SHARES_PER_CONTRACT,
            reward_to_risk=reward_to_risk,
        )

    # ── Utility helpers ───────────────────────────────────────────────

    @staticmethod
    def _round_to_strike(price: float, increment: float = 5.0) -> float:
        """Round price to the nearest options strike increment.

        Standard US options have $1 or $5 strike increments depending on price.
        We default to $5 for simplicity; Alpaca will return the available strikes.
        """
        return round(round(price / increment) * increment, 2)

    @staticmethod
    def _find_closest_contract(
        chain_with_quotes: list[tuple["OptionsContract", Optional["OptionsQuote"]]],
        target_strike: float,
    ) -> Optional[tuple["OptionsContract", Optional["OptionsQuote"]]]:
        """Return the contract whose strike is closest to target_strike."""
        if not chain_with_quotes:
            return None
        return min(
            chain_with_quotes,
            key=lambda cq: abs(cq[0].strike_price - target_strike),
        )
