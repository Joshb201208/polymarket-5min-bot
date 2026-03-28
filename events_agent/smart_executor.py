"""Smart order execution — scaled entry, limit orders, TWAP exits."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from events_agent.config import EventsConfig
from events_agent.models import EdgeResult, Position, Trade
from nba_agent.utils import utcnow, atomic_json_write, load_json

logger = logging.getLogger(__name__)


@dataclass
class PendingTranche:
    """A scheduled order tranche waiting to be executed."""
    id: str
    market_id: str
    market_question: str
    token_id: str
    side: str
    side_index: int
    size: float
    direction: str
    scheduled_at: str       # ISO timestamp when it should fire
    created_at: str
    conditions: dict = field(default_factory=dict)
    status: str = "pending"  # pending, executed, cancelled, expired
    parent_position_id: str = ""
    tranche_type: str = "entry"  # entry or exit

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PendingTranche:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class SmartExecutor:
    """Intelligent order execution for events markets.

    Features:
    - Scaled entry: positions >$20 split into 2 tranches (60/40)
    - Smart limit orders: place at best bid + 1 tick
    - TWAP exits: larger positions ($30+) sold in 3 tranches over 30 min
    - Pending tranche system: scheduled tranches written to disk
    """

    # Thresholds (overridable via env)
    SCALED_ENTRY_THRESHOLD = float(
        __import__("os").getenv("EXECUTION_SCALED_ENTRY_THRESHOLD", "20")
    )
    TRANCHE_DELAY_MINUTES = int(
        __import__("os").getenv("EXECUTION_TRANCHE_DELAY_MINUTES", "15")
    )
    TWAP_TRANCHES = int(
        __import__("os").getenv("EXECUTION_TWAP_TRANCHES", "3")
    )
    TWAP_INTERVAL_MINUTES = int(
        __import__("os").getenv("EXECUTION_TWAP_INTERVAL_MINUTES", "10")
    )

    def __init__(self, config: EventsConfig | None = None) -> None:
        self.config = config or EventsConfig()
        self._pending_path = self.config.DATA_DIR / "pending_tranches.json"
        self.config.ensure_data_dir()

    # ------------------------------------------------------------------
    # Entry execution
    # ------------------------------------------------------------------

    def execute_entry(
        self,
        edge_result: EdgeResult,
        bet_size: float,
        lifecycle=None,
    ) -> tuple[Position | None, Trade | None]:
        """Smart entry: scale into positions for larger bets.

        If bet_size < SCALED_ENTRY_THRESHOLD: buy all at once.
        If bet_size >= threshold: buy 60% immediately, schedule 40% after delay.
        """
        from events_agent.executor import EventsExecutor, _polymarket_taker_fee

        executor = EventsExecutor(self.config)

        if bet_size < self.SCALED_ENTRY_THRESHOLD:
            # Small bet — execute all at once
            return executor.execute_buy(edge_result, bet_size)

        # --- Scaled entry ---
        tranche1_size = round(bet_size * 0.6, 2)
        tranche2_size = round(bet_size * 0.4, 2)

        # Execute tranche 1 immediately
        position, trade = executor.execute_buy(edge_result, tranche1_size)
        if not position or not trade:
            return None, None

        logger.info(
            "SCALED ENTRY T1: $%.2f of $%.2f for %s",
            tranche1_size, bet_size, edge_result.market.question[:50],
        )

        # Schedule tranche 2
        market = edge_result.market
        side_index = edge_result.side_index
        token_id = market.clob_token_ids[side_index] if side_index < len(market.clob_token_ids) else ""
        from datetime import timedelta
        fire_at = utcnow() + timedelta(minutes=self.TRANCHE_DELAY_MINUTES)

        tranche = PendingTranche(
            id=f"tranche_{int(time.time())}_{market.id[:8]}",
            market_id=market.id,
            market_question=market.question,
            token_id=token_id,
            side=edge_result.side,
            side_index=side_index,
            size=tranche2_size,
            direction=edge_result.side,
            scheduled_at=fire_at.isoformat(),
            created_at=utcnow().isoformat(),
            conditions={
                "max_adverse_move": 0.02,
                "signal_must_be_active": True,
                "min_remaining_edge": 0.02,
                "entry_price": edge_result.market_price,
            },
            status="pending",
            parent_position_id=position.id,
            tranche_type="entry",
        )
        self._save_tranche(tranche)

        logger.info(
            "SCALED ENTRY T2 scheduled: $%.2f in %d min for %s",
            tranche2_size, self.TRANCHE_DELAY_MINUTES, market.question[:50],
        )

        return position, trade

    # ------------------------------------------------------------------
    # Exit execution
    # ------------------------------------------------------------------

    def execute_exit(
        self,
        position: Position,
        current_price: float,
        reason: str,
    ) -> Trade | None:
        """Smart exit: TWAP for larger positions, market sell for small ones."""
        from events_agent.executor import EventsExecutor

        executor = EventsExecutor(self.config)
        current_value = position.shares * current_price

        if current_value < 30:
            # Small position — sell all at once
            return executor.execute_sell(position, current_price, reason)

        # --- TWAP exit for larger positions ---
        logger.info(
            "TWAP EXIT: splitting $%.2f position into %d tranches for %s",
            current_value, self.TWAP_TRANCHES, position.market_question[:50],
        )

        # First tranche: sell immediately
        tranche_shares = round(position.shares / self.TWAP_TRANCHES, 4)
        first_trade = executor.execute_sell(position, current_price, f"{reason} (TWAP 1/{self.TWAP_TRANCHES})")

        # Schedule remaining tranches
        remaining_tranches = self.TWAP_TRANCHES - 1
        for i in range(remaining_tranches):
            from datetime import timedelta
            fire_at = utcnow() + timedelta(minutes=self.TWAP_INTERVAL_MINUTES * (i + 1))
            tranche = PendingTranche(
                id=f"twap_{int(time.time())}_{position.id[:8]}_{i}",
                market_id=position.market_id,
                market_question=position.market_question,
                token_id=position.token_id,
                side=position.side,
                side_index=0,
                size=tranche_shares,
                direction="SELL",
                scheduled_at=fire_at.isoformat(),
                created_at=utcnow().isoformat(),
                conditions={},
                status="pending",
                parent_position_id=position.id,
                tranche_type="exit",
            )
            self._save_tranche(tranche)

        return first_trade

    # ------------------------------------------------------------------
    # Pending tranche management
    # ------------------------------------------------------------------

    def load_pending_tranches(self) -> list[PendingTranche]:
        """Load all pending tranches from disk."""
        data = load_json(self._pending_path, {"tranches": []})
        tranches = []
        for d in data.get("tranches", []):
            try:
                tranches.append(PendingTranche.from_dict(d))
            except Exception as e:
                logger.warning("Failed to load tranche: %s", e)
        return tranches

    def get_ready_tranches(self) -> list[PendingTranche]:
        """Get tranches whose scheduled time has passed and are still pending."""
        now = utcnow()
        ready = []
        for t in self.load_pending_tranches():
            if t.status != "pending":
                continue
            try:
                fire_at = datetime.fromisoformat(t.scheduled_at.replace("Z", "+00:00"))
                if fire_at.tzinfo is None:
                    fire_at = fire_at.replace(tzinfo=timezone.utc)
                if fire_at <= now:
                    ready.append(t)
            except (ValueError, AttributeError):
                continue
        return ready

    async def execute_pending_tranches(self, scanner=None) -> list[Trade]:
        """Check and execute any ready pending tranches.

        Called each cycle from the main loop.
        """
        ready = self.get_ready_tranches()
        if not ready:
            return []

        from events_agent.executor import EventsExecutor
        executor = EventsExecutor(self.config)
        trades = []

        for tranche in ready:
            try:
                if tranche.tranche_type == "entry":
                    trade = await self._execute_entry_tranche(tranche, executor, scanner)
                else:
                    trade = await self._execute_exit_tranche(tranche, executor, scanner)

                if trade:
                    trades.append(trade)
                    self._update_tranche_status(tranche.id, "executed")
                    logger.info("Executed pending tranche %s", tranche.id)
                else:
                    self._update_tranche_status(tranche.id, "cancelled")
                    logger.info("Cancelled tranche %s (conditions not met)", tranche.id)
            except Exception as e:
                logger.error("Error executing tranche %s: %s", tranche.id, e)
                self._update_tranche_status(tranche.id, "cancelled")

        return trades

    async def _execute_entry_tranche(self, tranche: PendingTranche, executor, scanner) -> Trade | None:
        """Execute a delayed entry tranche if conditions are met."""
        conditions = tranche.conditions

        # Check adverse price movement
        if scanner and conditions.get("entry_price"):
            current_price = await scanner.get_market_price(tranche.token_id)
            if current_price is not None:
                entry_price = conditions["entry_price"]
                max_adverse = conditions.get("max_adverse_move", 0.02)
                # Buying YES: price going up is adverse (we pay more)
                # Buying NO: price going down is adverse
                if tranche.direction == "YES":
                    adverse_move = current_price - entry_price
                else:
                    adverse_move = entry_price - current_price

                if adverse_move > max_adverse:
                    logger.info(
                        "Tranche %s cancelled: adverse move %.2f%% > %.2f%%",
                        tranche.id, adverse_move * 100, max_adverse * 100,
                    )
                    return None

        # Execute the buy
        now_str = utcnow().isoformat()
        if self.config.is_paper:
            order_id = f"paper_evt_t2_{int(time.time())}"
            logger.info(
                "PAPER BUY T2: %s @ $%.2f | %s",
                tranche.side, tranche.size, tranche.market_question[:50],
            )
        else:
            # In live mode, use the executor's live buy
            order_id = executor._execute_live_buy(
                tranche.token_id, tranche.size, False,
            )
            if not order_id:
                return None

        trade = Trade(
            id=f"evt_trade_t2_{int(time.time())}",
            position_id=tranche.parent_position_id,
            market_id=tranche.market_id,
            market_question=tranche.market_question,
            action="BUY",
            side=tranche.side,
            price=0,  # Will be filled by market
            shares=0,
            amount=tranche.size,
            timestamp=now_str,
            mode="paper" if self.config.is_paper else "live",
            agent="events",
            order_id=order_id,
        )
        return trade

    async def _execute_exit_tranche(self, tranche: PendingTranche, executor, scanner) -> Trade | None:
        """Execute a TWAP exit tranche."""
        current_price = None
        if scanner:
            current_price = await scanner.get_market_price(tranche.token_id)

        if current_price is None:
            current_price = 0.50  # fallback

        now_str = utcnow().isoformat()
        if self.config.is_paper:
            order_id = f"paper_evt_twap_{int(time.time())}"
            logger.info(
                "PAPER SELL TWAP: %.2f shares @ %.2f¢ | %s",
                tranche.size, current_price * 100, tranche.market_question[:50],
            )
        else:
            order_id = executor._execute_live_sell(
                tranche.token_id, tranche.size, tranche.market_id,
            )
            if not order_id:
                return None

        exit_value = tranche.size * current_price

        trade = Trade(
            id=f"evt_trade_twap_{int(time.time())}",
            position_id=tranche.parent_position_id,
            market_id=tranche.market_id,
            market_question=tranche.market_question,
            action="SELL",
            side=tranche.side,
            price=current_price,
            shares=tranche.size,
            amount=round(exit_value, 2),
            timestamp=now_str,
            mode="paper" if self.config.is_paper else "live",
            agent="events",
            order_id=order_id,
        )
        return trade

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _save_tranche(self, tranche: PendingTranche) -> None:
        """Append a tranche to the pending file."""
        tranches = self.load_pending_tranches()
        tranches.append(tranche)
        self._write_tranches(tranches)

    def _update_tranche_status(self, tranche_id: str, status: str) -> None:
        """Update status of a specific tranche."""
        tranches = self.load_pending_tranches()
        for t in tranches:
            if t.id == tranche_id:
                t.status = status
                break
        self._write_tranches(tranches)

    def _write_tranches(self, tranches: list[PendingTranche]) -> None:
        """Write all tranches to disk."""
        data = {"tranches": [t.to_dict() for t in tranches]}
        atomic_json_write(self._pending_path, data)

    def cleanup_old_tranches(self) -> None:
        """Remove tranches older than 24 hours that are not pending."""
        from datetime import timedelta
        cutoff = utcnow() - timedelta(hours=24)
        tranches = self.load_pending_tranches()
        active = []
        for t in tranches:
            if t.status == "pending":
                active.append(t)
                continue
            try:
                created = datetime.fromisoformat(t.created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created > cutoff:
                    active.append(t)
            except (ValueError, AttributeError):
                pass
        if len(active) != len(tranches):
            self._write_tranches(active)
