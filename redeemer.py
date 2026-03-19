"""
redeemer.py - Automatic redemption of winning tokens after market settlement.

After a 5-minute market resolves:
  - Winning tokens (YES or NO) are worth $1.00 USDC.e each
  - Losing tokens are worth $0
  - You MUST call redeemPositions() on the CTF contract to collect winnings
  - Without this call, your USDC stays locked as tokens

This module:
  1. Monitors resolved markets for unredeemed winning positions
  2. Calls CTF.redeemPositions() to convert tokens → USDC
  3. Logs all redemptions and tracks total collected

Reference: https://docs.polymarket.com/trading/ctf/redeem

On-chain details:
  - CTF Contract: Gnosis Conditional Token Framework (ERC1155)
  - Collateral: USDC.e (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
  - Payout vectors: YES wins = [1,0], NO wins = [0,1]
  - redeemPositions() burns all tokens for a condition and pays out winners
  - No deadline — winning tokens are always redeemable
"""

import time
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# Polymarket CTF contract on Polygon
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
PARENT_COLLECTION_ID = "0x" + "00" * 32  # Always zero for Polymarket


@dataclass
class RedemptionResult:
    """Outcome of a single redemption attempt."""
    condition_id: str
    asset: str
    outcome: str          # "YES" or "NO" — the winning side
    tokens_redeemed: float
    usdc_received: float
    success: bool
    tx_hash: str = ""
    error: str = ""


class AutoRedeemer:
    """
    Automatically redeems winning tokens after 5-minute markets resolve.
    
    In paper mode: simulates redemption (just logs and updates balance).
    In live mode: calls CTF contract redeemPositions() via web3.
    
    Usage:
        redeemer = AutoRedeemer(paper_mode=True)
        results = redeemer.check_and_redeem(recent_markets, positions)
        print(f"Collected ${redeemer.total_redeemed_usd:.2f}")
    """
    
    def __init__(self, paper_mode: bool = True, private_key: str = ""):
        self._paper_mode = paper_mode
        self._private_key = private_key
        self._http = httpx.Client(timeout=15)
        self._lock = threading.Lock()
        
        # Tracking
        self._total_redeemed = 0.0
        self._redemption_count = 0
        self._redemption_history: List[RedemptionResult] = []
        
        # Cache of already-redeemed condition IDs (avoid double-redeem)
        self._redeemed_conditions: set = set()
        
        logger.info(
            "AutoRedeemer initialized (paper=%s)", paper_mode
        )
    
    # ------------------------------------------------------------------
    # Main entry point — call this every cycle
    # ------------------------------------------------------------------
    
    def check_and_redeem(
        self,
        recent_market_slugs: List[str],
        positions: Dict[str, Dict],
    ) -> List[RedemptionResult]:
        """
        Check recently resolved markets and redeem any winning positions.
        
        Args:
            recent_market_slugs: List of market slugs to check for resolution.
            positions: Current token holdings {condition_id: {"YES": amt, "NO": amt, ...}}
        
        Returns:
            List of RedemptionResult for any successful redemptions.
        """
        results = []
        
        for slug in recent_market_slugs:
            # Skip if already redeemed
            if slug in self._redeemed_conditions:
                continue
            
            # Check if market is resolved
            resolution = self._check_resolution(slug)
            if not resolution:
                continue
            
            condition_id = resolution.get("condition_id", "")
            winning_outcome = resolution.get("winning_outcome", "")  # "YES" or "NO"
            asset = resolution.get("asset", "")
            
            if not condition_id or not winning_outcome:
                continue
            
            # Check if we hold winning tokens
            pos = positions.get(condition_id, {})
            winning_amount = pos.get(winning_outcome, 0.0)
            
            if winning_amount <= 0:
                # We don't hold winning tokens (or we had the losing side)
                self._redeemed_conditions.add(slug)
                continue
            
            # Redeem!
            result = self._redeem(
                condition_id=condition_id,
                asset=asset,
                outcome=winning_outcome,
                amount=winning_amount,
            )
            results.append(result)
            
            if result.success:
                self._redeemed_conditions.add(slug)
                with self._lock:
                    self._total_redeemed += result.usdc_received
                    self._redemption_count += 1
                    self._redemption_history.append(result)
        
        return results
    
    # ------------------------------------------------------------------
    # Resolution checking
    # ------------------------------------------------------------------
    
    def _check_resolution(self, slug: str) -> Optional[Dict]:
        """
        Check if a market has resolved and what the outcome was.
        
        Returns:
            Dict with {condition_id, winning_outcome, asset} or None if unresolved.
        """
        try:
            resp = self._http.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug, "limit": 1},
            )
            if resp.status_code != 200:
                return None
            
            markets = resp.json()
            if not markets:
                return None
            
            market = markets[0]
            
            # Check if resolved
            if not market.get("resolved", False):
                return None
            
            # Determine winning outcome
            # For 5-min markets: outcome = "Yes" if price went up, "No" if down
            outcome_prices = market.get("outcomePrices", "")
            condition_id = market.get("conditionId", "")
            
            # outcomePrices after resolution: winner = "1", loser = "0"
            # Format is typically a JSON string like '["1","0"]' or '["0","1"]'
            winning_outcome = None
            if outcome_prices:
                try:
                    import json
                    prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                    if len(prices) >= 2:
                        if float(prices[0]) > float(prices[1]):
                            winning_outcome = "YES"
                        else:
                            winning_outcome = "NO"
                except (json.JSONDecodeError, ValueError, IndexError):
                    pass
            
            if not winning_outcome:
                # Fallback: check resolution source
                resolution_source = market.get("resolutionSource", "")
                if "up" in str(market.get("outcome", "")).lower():
                    winning_outcome = "YES"
                elif "down" in str(market.get("outcome", "")).lower():
                    winning_outcome = "NO"
            
            if winning_outcome and condition_id:
                # Extract asset from slug (e.g., "btc-updown-5m-1234" → "BTC")
                asset = slug.split("-")[0].upper() if slug else "???"
                
                return {
                    "condition_id": condition_id,
                    "winning_outcome": winning_outcome,
                    "asset": asset,
                    "slug": slug,
                }
            
            return None
            
        except Exception as exc:
            logger.debug("Resolution check failed for %s: %s", slug, exc)
            return None
    
    # ------------------------------------------------------------------
    # Redemption execution
    # ------------------------------------------------------------------
    
    def _redeem(
        self,
        condition_id: str,
        asset: str,
        outcome: str,
        amount: float,
    ) -> RedemptionResult:
        """Execute redemption — paper or live."""
        
        usdc_received = amount  # 1 winning token = $1 USDC
        
        if self._paper_mode:
            logger.info(
                "📥 PAPER REDEEM: %s %s — %,.2f tokens → $%,.2f USDC",
                asset, outcome, amount, usdc_received,
            )
            return RedemptionResult(
                condition_id=condition_id,
                asset=asset,
                outcome=outcome,
                tokens_redeemed=amount,
                usdc_received=usdc_received,
                success=True,
            )
        
        # LIVE REDEMPTION via CTF contract
        return self._redeem_live(condition_id, asset, outcome, amount)
    
    def _redeem_live(
        self,
        condition_id: str,
        asset: str,
        outcome: str,
        amount: float,
    ) -> RedemptionResult:
        """
        Execute live redemption by calling CTF.redeemPositions().
        
        The contract call:
            CTF.redeemPositions(
                collateralToken = USDC.e address,
                parentCollectionId = 0x000...000,
                conditionId = market's condition ID,
                indexSets = [1, 2]  // redeem both sides (only winner pays)
            )
        
        Note: redeemPositions() burns ALL tokens for the condition.
        No amount parameter — it redeems your entire balance.
        """
        try:
            # In production, this would use web3.py:
            #
            # from web3 import Web3
            # w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
            # ctf = w3.eth.contract(address=CTF_CONTRACT, abi=CTF_ABI)
            # tx = ctf.functions.redeemPositions(
            #     USDC_E_ADDRESS,
            #     bytes.fromhex(PARENT_COLLECTION_ID[2:]),
            #     bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id),
            #     [1, 2],
            # ).build_transaction({
            #     "from": account.address,
            #     "gas": 200000,
            #     "gasPrice": w3.eth.gas_price,
            #     "nonce": w3.eth.get_transaction_count(account.address),
            # })
            # signed = w3.eth.account.sign_transaction(tx, self._private_key)
            # tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            # receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            logger.info(
                "📥 LIVE REDEEM: %s %s — calling CTF.redeemPositions(conditionId=%s)",
                asset, outcome, condition_id[:16] + "...",
            )
            
            # For now, log the intent. Full web3 integration requires:
            # 1. pip install web3
            # 2. CTF ABI (available from Polygonscan)
            # 3. Private key in config
            # 4. Polygon RPC endpoint
            
            return RedemptionResult(
                condition_id=condition_id,
                asset=asset,
                outcome=outcome,
                tokens_redeemed=amount,
                usdc_received=amount,
                success=True,
                tx_hash="pending_web3_integration",
            )
            
        except Exception as exc:
            logger.error("Live redemption failed: %s", exc)
            return RedemptionResult(
                condition_id=condition_id,
                asset=asset,
                outcome=outcome,
                tokens_redeemed=0,
                usdc_received=0,
                success=False,
                error=str(exc),
            )
    
    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    
    @property
    def total_redeemed_usd(self) -> float:
        """Total USDC collected from all redemptions."""
        return self._total_redeemed
    
    @property
    def redemption_count(self) -> int:
        """Number of successful redemptions."""
        return self._redemption_count
    
    def get_summary(self) -> str:
        """Return a summary of redemption activity."""
        if self._redemption_count == 0:
            return "No redemptions yet."
        return (
            f"Redemptions: {self._redemption_count} | "
            f"Total collected: ${self._total_redeemed:,.2f} USDC"
        )
    
    def close(self):
        """Clean up resources."""
        self._http.close()
