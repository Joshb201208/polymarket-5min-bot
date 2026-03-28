"""Tier 1B: Polymarket WebSocket Orderbook Intelligence.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market (no auth required).
Detects whale trades, orderbook imbalances, sharp moves, spread opportunities, volume spikes.

Designed to run as a long-lived asyncio task — NOT polled every scan cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from nba_agent.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.orderbook")

# Maximum history entries per token
_MAX_PRICE_HISTORY = 600  # ~10 min at 1/sec
_MAX_TRADE_HISTORY = 200


class OrderbookIntelligence:
    """Real-time orderbook intelligence via Polymarket WebSocket."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._ws = None
        self._connected = False
        self._shutdown = False

        # Internal state per token_id
        self.orderbooks: dict[str, dict] = {}  # token_id -> {bids: [], asks: []}
        self.price_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=_MAX_PRICE_HISTORY)
        )
        self.trade_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=_MAX_TRADE_HISTORY)
        )
        self.volume_15min: dict[str, float] = defaultdict(float)
        self.volume_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=96)  # ~24h at 15-min windows
        )

        # Token-to-market mapping
        self._token_market_map: dict[str, dict] = {}

        # Pending signals (consumed by manager each cycle)
        self._pending_signals: list[Signal] = []
        self._signals_lock = asyncio.Lock()

        # Signals log path
        self._signals_path = self.config.DATA_DIR / "orderbook_signals.json"

    def register_tokens(self, token_market_map: dict[str, dict]) -> None:
        """Register token_id -> {market_id, market_question} mapping."""
        self._token_market_map.update(token_market_map)

    async def connect(self, token_ids: list[str]) -> None:
        """Connect to Polymarket WebSocket and subscribe to tokens."""
        if not self.config.is_enabled("orderbook"):
            logger.debug("Orderbook intelligence disabled")
            return

        if not token_ids:
            logger.info("No token IDs to subscribe to")
            return

        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed — orderbook disabled")
            return

        ws_url = self.config.ORDERBOOK_WS_URL
        logger.info("Connecting to Polymarket WS: %s (%d tokens)", ws_url, len(token_ids))

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(ws_url, ping_interval=30, ping_timeout=10),
                timeout=15,
            )
            self._connected = True

            # Subscribe to each token's market channel
            for token_id in token_ids:
                subscribe_msg = json.dumps({
                    "type": "market",
                    "assets_ids": [token_id],
                })
                await self._ws.send(subscribe_msg)

            logger.info("WebSocket connected, subscribed to %d tokens", len(token_ids))
        except Exception as e:
            logger.error("WebSocket connection failed: %s", e)
            self._connected = False

    async def run(self) -> None:
        """Main loop processing WebSocket messages. Runs as a persistent task."""
        if not self.config.is_enabled("orderbook"):
            return

        reconnect_delay = 5
        while not self._shutdown:
            try:
                if not self._connected or self._ws is None:
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)
                    continue

                reconnect_delay = 5  # Reset on successful connection

                async for raw_msg in self._ws:
                    if self._shutdown:
                        break
                    try:
                        msg = json.loads(raw_msg)
                        await self._process_message(msg)
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        logger.warning("Error processing WS message: %s", e)

            except Exception as e:
                logger.error("WebSocket error (will reconnect): %s", e)
                self._connected = False
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

                # Attempt reconnect
                token_ids = list(self._token_market_map.keys())
                if token_ids:
                    try:
                        await self.connect(token_ids)
                    except Exception:
                        pass

    async def _process_message(self, msg: dict) -> None:
        """Process a single WebSocket message and update internal state."""
        event_type = msg.get("event_type", "")
        asset_id = msg.get("asset_id", "")

        if event_type == "book":
            self._update_book(asset_id, msg)
        elif event_type == "price_change":
            self._record_price(asset_id, msg)
        elif event_type == "last_trade_price":
            self._record_trade(asset_id, msg)
        elif event_type == "tick_size_change":
            pass  # Ignore
        elif event_type == "best_bid_ask":
            self._update_bba(asset_id, msg)

        # Check for signals after processing
        if asset_id:
            new_signals = self._check_signals(asset_id)
            if new_signals:
                async with self._signals_lock:
                    self._pending_signals.extend(new_signals)

    def _update_book(self, token_id: str, msg: dict) -> None:
        """Update orderbook state for a token."""
        self.orderbooks[token_id] = {
            "bids": msg.get("bids", []),
            "asks": msg.get("asks", []),
            "timestamp": utcnow().isoformat(),
        }

    def _record_price(self, token_id: str, msg: dict) -> None:
        """Record a price change event."""
        price = msg.get("price")
        if price is not None:
            self.price_history[token_id].append({
                "timestamp": utcnow(),
                "price": float(price),
            })

    def _record_trade(self, token_id: str, msg: dict) -> None:
        """Record a trade event."""
        now = utcnow()
        price = msg.get("price")
        size = msg.get("size")
        if price is not None and size is not None:
            trade_value = float(price) * float(size)
            self.trade_history[token_id].append({
                "timestamp": now,
                "price": float(price),
                "size": float(size),
                "value": trade_value,
            })
            self.volume_15min[token_id] += trade_value

    def _update_bba(self, token_id: str, msg: dict) -> None:
        """Update best bid/ask state."""
        book = self.orderbooks.get(token_id, {})
        best_bid = msg.get("best_bid")
        best_ask = msg.get("best_ask")
        if best_bid is not None:
            book["best_bid"] = float(best_bid)
        if best_ask is not None:
            book["best_ask"] = float(best_ask)
        self.orderbooks[token_id] = book

    def _check_signals(self, token_id: str) -> list[Signal]:
        """Check all signal conditions for a token. Returns list of new Signals."""
        signals: list[Signal] = []
        now = utcnow()
        market_info = self._token_market_map.get(token_id, {})
        market_id = market_info.get("market_id", token_id)
        market_question = market_info.get("market_question", "")

        # 1. WHALE DETECTION: single trade > $5K
        trades = self.trade_history.get(token_id, deque())
        if trades:
            latest = trades[-1]
            if latest["value"] > self.config.WHALE_TRADE_THRESHOLD:
                signals.append(Signal(
                    source="orderbook",
                    market_id=market_id,
                    market_question=market_question,
                    signal_type="whale_trade",
                    direction="YES" if latest["price"] > 0.5 else "NO",
                    strength=min(latest["value"] / 20000, 1.0),
                    confidence=0.6,
                    details={
                        "trade_value": latest["value"],
                        "trade_price": latest["price"],
                        "trade_size": latest["size"],
                    },
                    timestamp=now,
                    expires_at=now + timedelta(minutes=15),
                ))

        # 2. ORDERBOOK IMBALANCE: bid_total / ask_total ratio
        book = self.orderbooks.get(token_id, {})
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            bid_total = sum(
                float(b["size"]) * float(b["price"]) if isinstance(b, dict)
                else float(b[1]) * float(b[0])
                for b in bids
            )
            ask_total = sum(
                float(a["size"]) * float(a["price"]) if isinstance(a, dict)
                else float(a[1]) * float(a[0])
                for a in asks
            )

            if ask_total > 0:
                ratio = bid_total / ask_total
                if ratio > self.config.IMBALANCE_RATIO or ratio < (1.0 / self.config.IMBALANCE_RATIO):
                    direction = "YES" if ratio > 1.0 else "NO"
                    signals.append(Signal(
                        source="orderbook",
                        market_id=market_id,
                        market_question=market_question,
                        signal_type="orderbook_imbalance",
                        direction=direction,
                        strength=min(max(ratio, 1.0 / ratio) / 10.0, 1.0),
                        confidence=0.5,
                        details={
                            "bid_total": round(bid_total, 2),
                            "ask_total": round(ask_total, 2),
                            "ratio": round(ratio, 2),
                        },
                        timestamp=now,
                        expires_at=now + timedelta(minutes=10),
                    ))

        # 3. SHARP MOVE: price moved > 3% in last 10 minutes
        prices = self.price_history.get(token_id, deque())
        if len(prices) >= 2:
            cutoff = now - timedelta(minutes=10)
            recent = [p for p in prices if p["timestamp"] >= cutoff]
            if len(recent) >= 2:
                first_price = recent[0]["price"]
                last_price = recent[-1]["price"]
                if first_price > 0:
                    move_pct = abs(last_price - first_price) / first_price
                    if move_pct > self.config.SHARP_MOVE_PCT:
                        direction = "YES" if last_price > first_price else "NO"
                        signals.append(Signal(
                            source="orderbook",
                            market_id=market_id,
                            market_question=market_question,
                            signal_type="sharp_move",
                            direction=direction,
                            strength=min(move_pct / 0.10, 1.0),
                            confidence=0.7,
                            details={
                                "move_pct": round(move_pct * 100, 2),
                                "from_price": first_price,
                                "to_price": last_price,
                                "minutes": 10,
                            },
                            timestamp=now,
                            expires_at=now + timedelta(minutes=15),
                        ))

        # 4. SPREAD OPPORTUNITY: bid-ask spread > 5 cents
        best_bid = book.get("best_bid")
        best_ask = book.get("best_ask")
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid
            if spread > self.config.SPREAD_OPPORTUNITY_THRESHOLD:
                signals.append(Signal(
                    source="orderbook",
                    market_id=market_id,
                    market_question=market_question,
                    signal_type="spread_opportunity",
                    direction="NEUTRAL",
                    strength=min(spread / 0.15, 1.0),
                    confidence=0.8,
                    details={
                        "spread": round(spread, 4),
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                    },
                    timestamp=now,
                    expires_at=now + timedelta(minutes=5),
                ))

        # 5. VOLUME SPIKE: 15-min volume > 3x average
        vol_history = self.volume_history.get(token_id, deque())
        current_vol = self.volume_15min.get(token_id, 0)
        if len(vol_history) >= 4 and current_vol > 0:
            avg_vol = sum(vol_history) / len(vol_history)
            if avg_vol > 0 and current_vol > avg_vol * self.config.VOLUME_SPIKE_MULTIPLIER:
                signals.append(Signal(
                    source="orderbook",
                    market_id=market_id,
                    market_question=market_question,
                    signal_type="volume_spike",
                    direction="NEUTRAL",
                    strength=min(current_vol / (avg_vol * 10), 1.0),
                    confidence=0.5,
                    details={
                        "current_volume": round(current_vol, 2),
                        "avg_volume": round(avg_vol, 2),
                        "multiplier": round(current_vol / avg_vol, 1),
                    },
                    timestamp=now,
                    expires_at=now + timedelta(minutes=15),
                ))

        return signals

    async def scan(self, active_markets: list) -> list[Signal]:
        """REST-based fallback scan using CLOB /book endpoint.

        When the WebSocket is connected, signals come via get_pending_signals().
        This scan() method provides a fallback that queries the CLOB REST API
        for orderbook depth, detecting imbalances and spread opportunities.
        """
        if not self.config.is_enabled("orderbook"):
            return []

        # If WebSocket is connected and producing data, skip REST fallback
        if self._connected and self.orderbooks:
            logger.debug("Orderbook WS active — skipping REST fallback scan")
            return []

        signals: list[Signal] = []
        now = utcnow()

        # Query CLOB book endpoint for each market's token
        async with httpx.AsyncClient(timeout=10.0) as client:
            for market in active_markets[:20]:  # Limit to avoid rate limiting
                token_id = ""
                tokens = getattr(market, "clob_token_ids", None) or getattr(market, "token_ids", None)
                if tokens and isinstance(tokens, list) and tokens:
                    token_id = tokens[0]
                if not token_id:
                    continue

                market_id = getattr(market, "id", "")
                question = getattr(market, "question", "")

                try:
                    resp = await client.get(
                        f"https://clob.polymarket.com/book",
                        params={"token_id": token_id},
                    )
                    if resp.status_code != 200:
                        continue
                    book_data = resp.json()
                except Exception:
                    continue

                bids = book_data.get("bids", [])
                asks = book_data.get("asks", [])
                if not bids or not asks:
                    continue

                # Calculate depth
                bid_total = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids)
                ask_total = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks)

                # Imbalance detection
                if ask_total > 0:
                    ratio = bid_total / ask_total
                    if ratio > self.config.IMBALANCE_RATIO or ratio < (1.0 / self.config.IMBALANCE_RATIO):
                        direction = "YES" if ratio > 1.0 else "NO"
                        signals.append(Signal(
                            source="orderbook",
                            market_id=market_id,
                            market_question=question,
                            signal_type="orderbook_imbalance",
                            direction=direction,
                            strength=min(max(ratio, 1.0 / ratio) / 10.0, 1.0),
                            confidence=0.5,
                            details={
                                "bid_total": round(bid_total, 2),
                                "ask_total": round(ask_total, 2),
                                "ratio": round(ratio, 2),
                                "source": "rest_fallback",
                            },
                            timestamp=now,
                            expires_at=now + timedelta(minutes=10),
                        ))

                # Spread detection
                if bids and asks:
                    best_bid = max(float(b.get("price", 0)) for b in bids)
                    best_ask = min(float(a.get("price", 0)) for a in asks)
                    spread = best_ask - best_bid
                    if spread > self.config.SPREAD_OPPORTUNITY_THRESHOLD:
                        signals.append(Signal(
                            source="orderbook",
                            market_id=market_id,
                            market_question=question,
                            signal_type="spread_opportunity",
                            direction="NEUTRAL",
                            strength=min(spread / 0.15, 1.0),
                            confidence=0.8,
                            details={
                                "spread": round(spread, 4),
                                "best_bid": best_bid,
                                "best_ask": best_ask,
                                "source": "rest_fallback",
                            },
                            timestamp=now,
                            expires_at=now + timedelta(minutes=5),
                        ))

                await asyncio.sleep(0.2)  # Rate limit

        if signals:
            logger.info("Orderbook REST fallback: %d signals from %d markets", len(signals), len(active_markets))
        return signals

    def get_pending_signals(self) -> list[Signal]:
        """Return and clear pending signals. Called by IntelligenceManager each cycle."""
        # Use a simple swap since this is called from the same event loop
        signals = self._pending_signals
        self._pending_signals = []
        return signals

    def get_depth_summary(self, token_id: str) -> dict:
        """Return orderbook depth summary for dashboard display."""
        book = self.orderbooks.get(token_id, {})
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        bid_total = sum(
            float(b["size"]) * float(b["price"]) if isinstance(b, dict)
            else float(b[1]) * float(b[0])
            for b in bids
        ) if bids else 0.0

        ask_total = sum(
            float(a["size"]) * float(a["price"]) if isinstance(a, dict)
            else float(a[1]) * float(a[0])
            for a in asks
        ) if asks else 0.0

        best_bid = book.get("best_bid", 0)
        best_ask = book.get("best_ask", 0)
        spread = (best_ask - best_bid) if best_bid and best_ask else 0

        return {
            "bid_total": round(bid_total, 2),
            "ask_total": round(ask_total, 2),
            "spread": round(spread, 4),
            "top_bid": best_bid,
            "top_ask": best_ask,
            "depth_ratio": round(bid_total / ask_total, 2) if ask_total > 0 else 0,
        }

    def rotate_volume_window(self) -> None:
        """Rotate 15-minute volume window. Call this every 15 minutes."""
        for token_id in list(self.volume_15min.keys()):
            self.volume_history[token_id].append(self.volume_15min[token_id])
            self.volume_15min[token_id] = 0.0

    async def shutdown(self) -> None:
        """Gracefully close WebSocket connection."""
        self._shutdown = True
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._connected = False
        logger.info("Orderbook WebSocket shut down")
