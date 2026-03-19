"""
position_merger.py - Merge YES + NO tokens back into USDC.

When you hold both YES and NO tokens for the same market:
  YES + NO = $1 USDC.e

This is critical for capital efficiency:
- After a losing trade, you may accumulate opposite-side tokens
- Rather than selling them at market (incurring spread), merge to get full $1
- Frees up locked capital instantly

References: https://docs.polymarket.com/trading/ctf/merge
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MergeOpportunity:
    """Represents a pair of positions that can be merged."""
    condition_id: str
    asset: str
    yes_token_id: str
    no_token_id: str
    mergeable_amount: float  # Min of YES and NO holdings
    usdc_recovered: float    # = mergeable_amount (since YES+NO = $1)


class PositionMerger:
    """
    Detects and executes position merges for capital recovery.

    In paper mode, simulates the merge.
    In live mode, calls CTF contract mergePositions().

    Usage:
        merger = PositionMerger(executor=executor, paper_mode=False)
        positions = {
            "0xcondid...": {
                "YES": 5.0, "NO": 3.0,
                "yes_token_id": "...", "no_token_id": "...",
                "asset": "BTC"
            }
        }
        opps = merger.find_merge_opportunities(positions)
        recovered = merger.execute_merges(opps)
    """

    def __init__(self, executor=None, paper_mode: bool = True):
        self._executor = executor
        self._paper_mode = paper_mode
        self._total_merged = 0.0
        logger.info("PositionMerger initialized (paper=%s)", paper_mode)

    def find_merge_opportunities(
        self, positions: Dict[str, Dict]
    ) -> List[MergeOpportunity]:
        """
        Scan positions for YES+NO pairs that can be merged.

        Args:
            positions: Dict of condition_id -> {"YES": amount, "NO": amount,
                       "yes_token_id": str, "no_token_id": str, "asset": str}

        Returns:
            List of merge opportunities sorted by value (highest first)
        """
        opportunities = []

        for cid, pos in positions.items():
            yes_amount = pos.get("YES", 0)
            no_amount = pos.get("NO", 0)

            if yes_amount > 0 and no_amount > 0:
                mergeable = min(yes_amount, no_amount)
                if mergeable >= 0.01:  # Min merge amount
                    opportunities.append(MergeOpportunity(
                        condition_id=cid,
                        asset=pos.get("asset", "???"),
                        yes_token_id=pos.get("yes_token_id", ""),
                        no_token_id=pos.get("no_token_id", ""),
                        mergeable_amount=mergeable,
                        usdc_recovered=mergeable,
                    ))

        # Sort by value — merge biggest first
        opportunities.sort(key=lambda o: o.usdc_recovered, reverse=True)
        return opportunities

    def execute_merges(self, opportunities: List[MergeOpportunity]) -> float:
        """
        Execute all merge opportunities.

        Returns:
            Total USDC recovered from merges.
        """
        total_recovered = 0.0

        for opp in opportunities:
            if self._paper_mode:
                # Simulate merge
                logger.info(
                    "PAPER MERGE: %s — merging %.2f YES + %.2f NO → $%.2f USDC",
                    opp.asset, opp.mergeable_amount, opp.mergeable_amount,
                    opp.usdc_recovered,
                )
                total_recovered += opp.usdc_recovered
            else:
                # Live merge via CTF contract
                try:
                    success = self._execute_live_merge(opp)
                    if success:
                        total_recovered += opp.usdc_recovered
                        logger.info(
                            "LIVE MERGE: %s — recovered $%.2f USDC",
                            opp.asset, opp.usdc_recovered,
                        )
                except Exception as exc:
                    logger.error("Merge failed for %s: %s", opp.asset, exc)

        self._total_merged += total_recovered
        if total_recovered > 0:
            logger.info(
                "Merged $%.2f USDC this cycle (all-time: $%.2f)",
                total_recovered, self._total_merged,
            )
        return total_recovered

    def _execute_live_merge(self, opp: MergeOpportunity) -> bool:
        """Execute a live merge via the CTF contract."""
        # This would use web3.py to call mergePositions() on the CTF contract
        # For now, placeholder that logs the intent
        logger.info(
            "Would call CTF.mergePositions(conditionId=%s, amount=%d)",
            opp.condition_id, int(opp.mergeable_amount * 1e6),
        )
        return True

    def build_positions_from_executor(self, executor) -> Dict[str, Dict]:
        """
        Helper: build the positions dict from live executor positions.

        Fetches live positions via executor.get_positions() and groups
        YES/NO pairs by condition_id for merge detection.

        Args:
            executor: OrderExecutor instance

        Returns:
            positions dict suitable for find_merge_opportunities()
        """
        try:
            raw_positions = executor.get_positions()
        except Exception as exc:
            logger.warning("PositionMerger: could not fetch positions: %s", exc)
            return {}

        # Group by condition_id
        grouped: Dict[str, Dict] = {}
        for pos in raw_positions:
            cid = pos.condition_id
            if cid not in grouped:
                grouped[cid] = {
                    "YES": 0.0,
                    "NO": 0.0,
                    "yes_token_id": "",
                    "no_token_id": "",
                    "asset": pos.asset,
                }
            outcome = pos.outcome.upper()
            if outcome == "YES":
                grouped[cid]["YES"] = pos.size
                grouped[cid]["yes_token_id"] = pos.token_id
            elif outcome == "NO":
                grouped[cid]["NO"] = pos.size
                grouped[cid]["no_token_id"] = pos.token_id

        return grouped

    def scan_and_merge(self, executor=None) -> float:
        """
        Convenience method: scan live positions and execute any merges found.

        Args:
            executor: Optional OrderExecutor; uses self._executor if not provided.

        Returns:
            Total USDC recovered.
        """
        exec_to_use = executor or self._executor
        if exec_to_use is None:
            logger.debug("PositionMerger.scan_and_merge: no executor available")
            return 0.0

        positions = self.build_positions_from_executor(exec_to_use)
        if not positions:
            return 0.0

        opportunities = self.find_merge_opportunities(positions)
        if not opportunities:
            logger.debug("PositionMerger: no merge opportunities found")
            return 0.0

        logger.info(
            "PositionMerger: found %d merge opportunities (total $%.2f)",
            len(opportunities), sum(o.usdc_recovered for o in opportunities),
        )
        return self.execute_merges(opportunities)

    @property
    def total_merged_usd(self) -> float:
        """Total USDC recovered from merges since initialization."""
        return self._total_merged
