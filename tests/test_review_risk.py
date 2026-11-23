"""Regression tests for cross-family risk review findings (RV-K-xx).

Helpers are reused from tests/test_risk_projection.py so the book shapes match
the owner's own tests.
"""

from decimal import Decimal as D

from stock_agent.config import Config
from stock_agent.risk_manager import RiskManager, TradeKind
from tests.test_risk_projection import option, snapshot, stock


def test_cash_secured_put_assignment_respects_sector_cap():
    risk = RiskManager(Config())
    book = snapshot(stock("BETA", 130, "Industrials"), stock("GAMA", 130, "Industrials"),
                    stock("DELT", 130, "Industrials"), cash="61000")
    trade = option(kind=TradeKind.CASH_SECURED_PUT, loss="3000", debit="0", sector="Industrials",
                   assignment_shares=D(100), assignment_strike=D(100))
    verdict = risk.evaluate_trade(book, trade)
    assert not verdict.ok and "sector_cap" in verdict.reasons, verdict


def test_control_same_exposure_as_a_stock_buy_is_refused_for_sector():
    """Control for RV-K-01: buying 5% more directly is refused by the sector cap."""
    from tests.test_risk_projection import buy
    risk = RiskManager(Config())
    book = snapshot(stock("BETA", 130, "Industrials"), stock("GAMA", 130, "Industrials"),
                    stock("DELT", 130, "Industrials"), cash="61000")
    assert risk.evaluate_trade(book, buy("ALFA", "50", sector="Industrials")).reasons == ("sector_cap",)
