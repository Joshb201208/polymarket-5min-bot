"""
ws_feed.py - WebSocket feed for real-time Polymarket orderbook data.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market
for streaming orderbook updates, trade prices, and best bid/ask.

This replaces REST polling for CLOB data — critical for competitive execution.
REST round-trips take 200-500ms; WebSocket delivers updates in <50ms.

The feed is OPTIONAL: if the WebSocket fails to connect or is unavailable,
the bot automatically falls back to REST polling via the OrderExecutor.
"""

import json
import time
import logging
import threading
from typing import Dict, Optional, List, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class OrderbookState:
    """Current state of an orderbook for a single token."""
    token_id: str
    best_bid: float = 0.0
    best_ask: float = 1.0
    mid_price: float = 0.5
    spread: float = 1.0
    last_trade_price: float = 0.0
    last_update_ts: float = 0.0
    bid_depth: List[List] = field(default_factory=list)  # [[price, size], ...]
    ask_depth: List[List] = field(default_factory=list)

    @property
    def is_stale(self) -> bool:
        """Data older than 30 seconds is considered stale."""
        return time.time() - self.last_update_ts > 30

    def update_from_book(self, bids: list, asks: list):
        """Update from a full book snapshot."""
        self.bid_depth = sorted(bids, key=lambda x: float(x[0]), reverse=True)
        self.ask_depth = sorted(asks, key=lambda x: float(x[0]))
        self._recalc()

    def update_best_bid_ask(self, best_bid: float, best_ask: float):
        """Update from best_bid_ask event."""
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.spread = best_ask - best_bid
        self.mid_price = (best_bid + best_ask) / 2.0
        self.last_update_ts = time.time()

    def update_last_trade(self, price: float):
        """Update from last_trade_price event."""
        self.last_trade_price = price
        self.last_update_ts = time.time()

    def _recalc(self):
        """Recalculate best bid/ask/mid/spread from depth."""
        if self.bid_depth:
            self.best_bid = float(self.bid_depth[0][0])
        if self.ask_depth:
            self.best_ask = float(self.ask_depth[0][0])
        self.spread = self.best_ask - self.best_bid
        self.mid_price = (self.best_bid + self.best_ask) / 2.0
        self.last_update_ts = time.time()


class WebSocketFeed:
    """
    Real-time orderbook feed via Polymarket WebSocket.

    Usage:
        feed = WebSocketFeed()
        feed.subscribe(["token_id_1", "token_id_2"])
        feed.start()

        # Get current state
        state = feed.get_orderbook("token_id_1")
        print(f"Best bid: {state.best_bid}, Best ask: {state.best_ask}")

    If WebSocket is unavailable, falls back to REST polling gracefully.
    Check feed.is_connected for current connection status.
    """

    def __init__(self):
        self._books: Dict[str, OrderbookState] = {}
        self._lock = threading.Lock()
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._subscribed_tokens: List[str] = []
        self._on_update_callbacks: List[Callable] = []
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._ws_available = True  # optimistically try; set False if import fails
        logger.info("WebSocketFeed initialized")

    @property
    def is_connected(self) -> bool:
        """Return True if WebSocket is currently connected."""
        return self._connected

    @property
    def is_available(self) -> bool:
        """Return True if websocket-client library is available."""
        return self._ws_available

    def subscribe(self, token_ids: List[str]):
        """Add tokens to subscribe to."""
        with self._lock:
            for tid in token_ids:
                if tid not in self._subscribed_tokens:
                    self._subscribed_tokens.append(tid)
                    self._books[tid] = OrderbookState(token_id=tid)

        # If already connected, send subscription
        if self._ws and self._connected:
            self._send_subscription(token_ids)

    def unsubscribe(self, token_ids: List[str]):
        """Remove tokens from subscription."""
        with self._lock:
            for tid in token_ids:
                self._subscribed_tokens = [t for t in self._subscribed_tokens if t != tid]
                self._books.pop(tid, None)

    def get_orderbook(self, token_id: str) -> Optional[OrderbookState]:
        """Get current orderbook state for a token."""
        with self._lock:
            return self._books.get(token_id)

    def get_mid_price(self, token_id: str) -> float:
        """Get current mid price (fast path). Returns 0.0 if not available."""
        book = self.get_orderbook(token_id)
        if book and not book.is_stale:
            return book.mid_price
        return 0.0

    def get_best_bid_ask(self, token_id: str) -> tuple:
        """
        Get current best bid and ask.
        Returns (0.0, 1.0) if not available or stale (caller should use REST fallback).
        """
        book = self.get_orderbook(token_id)
        if book and not book.is_stale:
            return book.best_bid, book.best_ask
        return 0.0, 1.0

    def on_update(self, callback: Callable):
        """Register callback for orderbook updates. callback(token_id, event_type, data)"""
        self._on_update_callbacks.append(callback)

    def start(self):
        """Start WebSocket connection in background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ws-feed")
        self._thread.start()
        logger.info("WebSocket feed started")

    def stop(self):
        """Stop WebSocket connection."""
        self._running = False
        self._connected = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("WebSocket feed stopped")

    def _run_loop(self):
        """Main WebSocket loop with automatic reconnection."""
        try:
            import websocket  # pip install websocket-client
        except ImportError:
            logger.warning(
                "websocket-client not installed — WebSocket feed disabled. "
                "Install with: pip install websocket-client"
            )
            self._ws_available = False
            self._running = False
            return

        while self._running:
            try:
                logger.info("Connecting to Polymarket WebSocket...")
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)

            except Exception as exc:
                logger.warning("WebSocket error: %s", exc)
                self._connected = False

            if self._running:
                logger.info("Reconnecting in %.0fs...", self._reconnect_delay)
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    def _on_open(self, ws):
        """Send subscription on connect."""
        logger.info("WebSocket connected")
        self._connected = True
        self._reconnect_delay = 1.0  # Reset backoff
        with self._lock:
            tokens = list(self._subscribed_tokens)
        if tokens:
            self._send_subscription(tokens)

    def _send_subscription(self, token_ids: List[str]):
        """Send subscription message for tokens."""
        if not self._ws:
            return
        msg = json.dumps({
            "type": "market",
            "assets_ids": token_ids,
            "custom_feature_enabled": True,  # Enables best_bid_ask events
        })
        try:
            self._ws.send(msg)
            logger.info("Subscribed to %d tokens via WebSocket", len(token_ids))
        except Exception as exc:
            logger.warning("Subscription failed: %s", exc)

    def _on_message(self, ws, message):
        """Process incoming WebSocket message."""
        try:
            data = json.loads(message)
            event_type = data.get("event_type", "")

            if event_type == "book":
                # Full orderbook snapshot
                asset_id = data.get("asset_id", "")
                with self._lock:
                    if asset_id in self._books:
                        self._books[asset_id].update_from_book(
                            data.get("bids", []),
                            data.get("asks", []),
                        )
                self._fire_callbacks(asset_id, event_type, data)

            elif event_type == "price_change":
                asset_id = data.get("asset_id", "")
                # Individual price level update
                with self._lock:
                    if asset_id in self._books:
                        # Update specific price level
                        side = data.get("side", "")
                        price = float(data.get("price", 0))
                        size = float(data.get("size", 0))
                        book = self._books[asset_id]
                        if side == "BUY":
                            book.bid_depth = self._update_level(book.bid_depth, price, size, reverse=True)
                        elif side == "SELL":
                            book.ask_depth = self._update_level(book.ask_depth, price, size, reverse=False)
                        book._recalc()
                self._fire_callbacks(asset_id, event_type, data)

            elif event_type == "best_bid_ask":
                asset_id = data.get("asset_id", "")
                with self._lock:
                    if asset_id in self._books:
                        self._books[asset_id].update_best_bid_ask(
                            float(data.get("best_bid", 0)),
                            float(data.get("best_ask", 1)),
                        )
                self._fire_callbacks(asset_id, event_type, data)

            elif event_type == "last_trade_price":
                asset_id = data.get("asset_id", "")
                with self._lock:
                    if asset_id in self._books:
                        self._books[asset_id].update_last_trade(
                            float(data.get("price", 0))
                        )
                self._fire_callbacks(asset_id, event_type, data)

            elif event_type == "market_resolved":
                # Market resolved — clean up
                asset_id = data.get("asset_id", "")
                logger.info("Market resolved: %s", asset_id)
                self._fire_callbacks(asset_id, event_type, data)

            elif event_type == "new_market":
                logger.info("New market detected: %s", data.get("asset_id", ""))
                self._fire_callbacks("", event_type, data)

        except Exception as exc:
            logger.debug("Message parse error: %s", exc)

    def _on_error(self, ws, error):
        logger.warning("WebSocket error: %s", error)
        self._connected = False

    def _on_close(self, ws, close_status_code, close_msg):
        logger.info("WebSocket closed (code=%s, msg=%s)", close_status_code, close_msg)
        self._connected = False

    @staticmethod
    def _update_level(depth: list, price: float, size: float, reverse: bool) -> list:
        """Update a price level in the orderbook depth."""
        # Remove existing level at this price
        depth = [level for level in depth if abs(float(level[0]) - price) > 1e-10]
        # Add new level if size > 0
        if size > 0:
            depth.append([str(price), str(size)])
        # Re-sort
        depth.sort(key=lambda x: float(x[0]), reverse=reverse)
        return depth

    def _fire_callbacks(self, token_id: str, event_type: str, data: dict):
        """Notify all registered callbacks."""
        for cb in self._on_update_callbacks:
            try:
                cb(token_id, event_type, data)
            except Exception as exc:
                logger.debug("Callback error: %s", exc)
