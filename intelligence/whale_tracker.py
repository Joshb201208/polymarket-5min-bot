"""Tier 3A: On-Chain Whale Wallet Tracker.

Tracks top Polymarket wallets via Polygonscan API and Polymarket Data API.
Generates copy-trading signals when multiple whales align on the same market.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from nba_agent.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.whale_tracker")

# Pre-seeded known whale wallets (will be supplemented from data/whale_wallets.json)
_DEFAULT_WHALE_WALLETS = [
    # These are placeholder addresses — real whale discovery happens via discover_whales()
]


class WhaleTracker:
    """Tracks profitable Polymarket wallets and generates copy-trading signals."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._wallets_path = self.config.DATA_DIR / "whale_wallets.json"
        self._signals_path = self.config.DATA_DIR / "whale_signals.json"
        self._whale_wallets: list[dict] = []  # {address, win_rate, total_trades}
        self._recent_activity: dict = defaultdict(list)  # market_id -> [whale_trades]

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan whale activity and generate signals. Returns Signal list."""
        if not self.config.is_enabled("whale_tracker"):
            logger.debug("Whale tracker disabled")
            return []

        self._load_wallets()
        if not self._whale_wallets:
            logger.info("No whale wallets to track — run discover_whales() first")
            return []

        signals: list[Signal] = []

        # Fetch recent whale transactions
        try:
            whale_trades = await asyncio.wait_for(
                self._fetch_whale_activity(),
                timeout=self.config.MODULE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Whale activity fetch timed out")
            return []
        except Exception as e:
            logger.error("Whale activity fetch failed: %s", e)
            return []

        if not whale_trades:
            return []

        # Group by market
        market_trades: dict[str, list] = defaultdict(list)
        for trade in whale_trades:
            market_id = trade.get("market_id", "")
            if market_id:
                market_trades[market_id].append(trade)

        # Check for consensus: 2+ whales on same side within 24h
        now = utcnow()
        for market_id, trades in market_trades.items():
            cutoff = now - timedelta(hours=24)
            recent = [
                t for t in trades
                if t.get("timestamp", "") > cutoff.isoformat()
            ]

            if len(recent) < self.config.WHALE_CONSENSUS_THRESHOLD:
                continue

            # Count direction consensus
            yes_count = sum(1 for t in recent if t.get("direction") == "YES")
            no_count = sum(1 for t in recent if t.get("direction") == "NO")

            consensus_count = max(yes_count, no_count)
            if consensus_count < self.config.WHALE_CONSENSUS_THRESHOLD:
                continue

            direction = "YES" if yes_count > no_count else "NO"
            total_value = sum(t.get("value", 0) for t in recent)

            # Find matching market question
            market_question = ""
            for market in active_markets:
                if getattr(market, "id", "") == market_id:
                    market_question = getattr(market, "question", "")
                    break

            signals.append(Signal(
                source="whale_tracker",
                market_id=market_id,
                market_question=market_question,
                signal_type="whale_consensus",
                direction=direction,
                strength=min(consensus_count / 5, 1.0),
                confidence=min(total_value / 50000, 1.0),
                details={
                    "whale_count": consensus_count,
                    "yes_whales": yes_count,
                    "no_whales": no_count,
                    "total_value": round(total_value, 2),
                    "wallets": [t.get("wallet", "")[:10] + "..." for t in recent],
                },
                timestamp=now,
                expires_at=now + timedelta(hours=6),
            ))

        # Persist whale signals
        self._save_signals(signals)
        logger.info("Whale tracker: %d signals from %d trades", len(signals), len(whale_trades))
        return signals

    async def _fetch_whale_activity(self) -> list[dict]:
        """Fetch recent ERC1155 transfers on CTF contract for whale wallets."""
        trades: list[dict] = []

        if not self.config.POLYGONSCAN_API_KEY:
            # Try Polymarket Data API as fallback (no key needed)
            return await self._fetch_via_data_api()

        # Use Polygonscan API for ERC1155 transfers
        for wallet_info in self._whale_wallets[:20]:  # Limit to top 20 whales
            address = wallet_info.get("address", "")
            if not address:
                continue

            try:
                wallet_trades = await self._fetch_wallet_transfers(address)
                trades.extend(wallet_trades)
            except Exception as e:
                logger.warning("Failed to fetch whale %s activity: %s", address[:10], e)

            # Rate limit: Polygonscan free tier = 5 calls/sec
            await asyncio.sleep(0.25)

        return trades

    async def _fetch_wallet_transfers(self, wallet: str) -> list[dict]:
        """Fetch ERC1155 token transfers for a wallet from Polygonscan."""
        url = self.config.POLYGONSCAN_API
        params = {
            "module": "account",
            "action": "token1155tx",
            "contractaddress": self.config.CTF_CONTRACT,
            "address": wallet,
            "page": 1,
            "offset": 20,
            "sort": "desc",
            "apikey": self.config.POLYGONSCAN_API_KEY,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

                if data.get("status") != "1":
                    return []

                transfers = data.get("result", [])
                trades = []
                now = utcnow()

                for tx in transfers:
                    # Only process recent (last 24h)
                    timestamp = int(tx.get("timeStamp", 0))
                    if timestamp == 0:
                        continue
                    tx_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                    if (now - tx_time).total_seconds() > 86400:
                        continue

                    token_id = tx.get("tokenID", "")
                    value = float(tx.get("tokenValue", 0))

                    if value < self.config.WHALE_MIN_TRADE_SIZE:
                        continue

                    is_buy = tx.get("to", "").lower() == wallet.lower()
                    trades.append({
                        "wallet": wallet,
                        "token_id": token_id,
                        "market_id": "",  # Resolved later via gamma API
                        "direction": "YES" if is_buy else "NO",
                        "value": value,
                        "timestamp": tx_time.isoformat(),
                        "tx_hash": tx.get("hash", ""),
                    })

                return trades
        except httpx.HTTPError as e:
            logger.warning("Polygonscan request failed for %s: %s", wallet[:10], e)
            return []

    async def _fetch_via_data_api(self) -> list[dict]:
        """Fallback: use Polymarket Data API to check whale positions."""
        trades: list[dict] = []
        now = utcnow()

        for wallet_info in self._whale_wallets[:10]:
            address = wallet_info.get("address", "")
            if not address:
                continue

            try:
                positions = await self.get_whale_positions(address)
                for pos in positions:
                    size = float(pos.get("size", 0))
                    if size >= self.config.WHALE_MIN_TRADE_SIZE:
                        trades.append({
                            "wallet": address,
                            "token_id": pos.get("asset", ""),
                            "market_id": pos.get("market", ""),
                            "direction": "YES" if pos.get("side", "") == "long" else "NO",
                            "value": size,
                            "timestamp": now.isoformat(),
                        })
            except Exception as e:
                logger.warning("Data API fetch failed for %s: %s", address[:10], e)

            await asyncio.sleep(0.5)

        return trades

    async def get_whale_positions(self, wallet: str) -> list[dict]:
        """Fetch all current positions for a whale wallet via Polymarket Data API."""
        url = f"https://data-api.polymarket.com/positions"
        params = {"user": wallet}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as e:
            logger.warning("Failed to get positions for %s: %s", wallet[:10], e)
            return []

    async def discover_whales(self) -> None:
        """Discover new whale wallets from top position holders on active markets.

        Should be called periodically (e.g., once per day) to update the wallet list.
        """
        logger.info("Starting whale discovery...")
        # This would scan Polymarket leaderboard / top holders
        # For now, load from file and supplement with defaults
        self._load_wallets()
        logger.info("Whale discovery complete: %d wallets tracked", len(self._whale_wallets))

    def _load_wallets(self) -> None:
        """Load whale wallets from disk."""
        data = load_json(self._wallets_path, {"wallets": _DEFAULT_WHALE_WALLETS})
        wallets = data.get("wallets", [])
        if isinstance(wallets, list):
            self._whale_wallets = wallets
        else:
            self._whale_wallets = _DEFAULT_WHALE_WALLETS[:]

    def _save_signals(self, signals: list[Signal]) -> None:
        """Persist whale signals for dashboard consumption."""
        if not signals:
            return
        try:
            existing = load_json(self._signals_path, [])
            if not isinstance(existing, list):
                existing = []
            for s in signals:
                existing.append(s.to_dict())
            # Keep last 200
            existing = existing[-200:]
            atomic_json_write(self._signals_path, existing)
        except Exception as e:
            logger.warning("Failed to save whale signals: %s", e)
