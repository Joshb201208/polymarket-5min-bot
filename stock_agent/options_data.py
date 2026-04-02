"""Options data feed — async HTTP client for Alpaca options market data.

Provides:
    - fetch_chain()      — list all tradable contracts for an underlying
    - fetch_snapshot()   — latest quotes and greeks for a set of contract symbols
    - get_option_quote() — convenience wrapper for a single contract
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import httpx

from stock_agent.config import Config
from stock_agent.options_models import (
    OptionType,
    OptionsContract,
    OptionsGreeks,
    OptionsQuote,
)

logger = logging.getLogger(__name__)

# Alpaca API endpoints
_TRADING_BASE = "https://paper-api.alpaca.markets"
_DATA_BASE = "https://data.alpaca.markets"


class OptionsDataFeed:
    """Async client for fetching options chain data and live quotes from Alpaca.

    Usage::

        feed = OptionsDataFeed(config)
        contracts = await feed.fetch_chain("AAPL", OptionType.CALL, min_dte=14, max_dte=45)
        quotes = await feed.fetch_snapshot([c.symbol for c in contracts[:10]])
        await feed.close()
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None

    # ── HTTP client lifecycle ────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        """Return a shared async HTTP client, creating one if needed."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _auth_headers(self) -> dict[str, str]:
        """Return Alpaca authentication headers."""
        return {
            "APCA-API-KEY-ID": self.config.ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": self.config.ALPACA_SECRET_KEY,
        }

    # ── Contract chain ───────────────────────────────────────────────

    async def fetch_chain(
        self,
        underlying: str,
        option_type: Optional[OptionType] = None,
        min_strike: Optional[float] = None,
        max_strike: Optional[float] = None,
        min_expiry: Optional[date] = None,
        max_expiry: Optional[date] = None,
        limit: int = 100,
    ) -> list[OptionsContract]:
        """Fetch all tradable options contracts for an underlying symbol.

        Args:
            underlying:   Ticker symbol, e.g. ``"AAPL"``.
            option_type:  Filter by ``OptionType.CALL`` or ``OptionType.PUT``.
                          If ``None``, both types are returned.
            min_strike:   Minimum strike price filter.
            max_strike:   Maximum strike price filter.
            min_expiry:   Earliest expiration date to include.
            max_expiry:   Latest expiration date to include.
            limit:        Maximum contracts per request (Alpaca caps at 10 000).

        Returns:
            List of :class:`OptionsContract` objects sorted by expiration then strike.
        """
        client = await self._get_client()
        params: dict[str, str | int] = {
            "underlying_symbols": underlying,
            "status": "active",
            "limit": limit,
        }

        if option_type is not None:
            params["type"] = option_type.value

        if min_strike is not None:
            params["strike_price_gte"] = str(min_strike)

        if max_strike is not None:
            params["strike_price_lte"] = str(max_strike)

        if min_expiry is not None:
            params["expiration_date_gte"] = min_expiry.isoformat()

        if max_expiry is not None:
            params["expiration_date_lte"] = max_expiry.isoformat()

        url = f"{_TRADING_BASE}/v2/options/contracts"
        all_contracts: list[OptionsContract] = []
        page_token: Optional[str] = None

        while True:
            if page_token:
                params["page_token"] = page_token

            try:
                resp = await client.get(url, params=params, headers=self._auth_headers())
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Alpaca contracts API error for %s: %s — %s",
                    underlying, exc.response.status_code, exc.response.text,
                )
                break
            except Exception as exc:
                logger.error("fetch_chain failed for %s: %s", underlying, exc)
                break

            contracts_raw: list[dict] = data.get("option_contracts", [])
            for raw in contracts_raw:
                try:
                    contract = OptionsContract(
                        id=raw["id"],
                        symbol=raw["symbol"],
                        name=raw.get("name", ""),
                        status=raw.get("status", "active"),
                        tradable=raw.get("tradable", True),
                        expiration_date=date.fromisoformat(raw["expiration_date"]),
                        root_symbol=raw.get("root_symbol", underlying),
                        underlying_symbol=raw.get("underlying_symbol", underlying),
                        type=OptionType(raw["type"]),
                        style=raw.get("style", "american"),
                        strike_price=float(raw["strike_price"]),
                        multiplier=int(raw.get("multiplier", 100)),
                        size=int(raw.get("size", 100)),
                        close_price=float(raw["close_price"]) if raw.get("close_price") else None,
                    )
                    if contract.tradable:
                        all_contracts.append(contract)
                except Exception as exc:
                    logger.warning("Failed to parse contract %s: %s", raw.get("symbol"), exc)

            # Pagination
            next_page = data.get("next_page_token")
            if not next_page:
                break
            page_token = next_page

        logger.info(
            "fetch_chain(%s, %s): retrieved %d contracts",
            underlying, option_type, len(all_contracts),
        )
        return sorted(all_contracts, key=lambda c: (c.expiration_date, c.strike_price))

    # ── Snapshot (quotes + greeks) ───────────────────────────────────

    async def fetch_snapshot(
        self,
        contract_symbols: list[str],
        feed: str = "indicative",
    ) -> dict[str, OptionsQuote]:
        """Fetch latest quotes and greeks for a list of contract symbols.

        Alpaca supports up to ~1 000 symbols per request.  Batching is handled
        internally for larger lists.

        Args:
            contract_symbols: List of OCC-format symbols, e.g.
                              ``["AAPL260418C00255000", "AAPL260418P00245000"]``.
            feed:             Alpaca market data feed — ``"indicative"`` or ``"opra"``.

        Returns:
            Mapping of contract symbol → :class:`OptionsQuote`.
        """
        if not contract_symbols:
            return {}

        client = await self._get_client()
        results: dict[str, OptionsQuote] = {}
        batch_size = 500  # Stay well under Alpaca limits

        for i in range(0, len(contract_symbols), batch_size):
            batch = contract_symbols[i : i + batch_size]
            params = {
                "symbols": ",".join(batch),
                "feed": feed,
            }
            url = f"{_DATA_BASE}/v1beta1/options/snapshots"

            try:
                resp = await client.get(url, params=params, headers=self._auth_headers())
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Alpaca snapshots API error: %s — %s",
                    exc.response.status_code, exc.response.text,
                )
                continue
            except Exception as exc:
                logger.error("fetch_snapshot failed: %s", exc)
                continue

            snapshots: dict[str, dict] = data.get("snapshots", {})
            for symbol, snap in snapshots.items():
                try:
                    quote = self._parse_snapshot(symbol, snap)
                    results[symbol] = quote
                except Exception as exc:
                    logger.warning("Failed to parse snapshot for %s: %s", symbol, exc)

        logger.debug("fetch_snapshot: returned quotes for %d / %d symbols", len(results), len(contract_symbols))
        return results

    def _parse_snapshot(self, symbol: str, snap: dict) -> OptionsQuote:
        """Parse a single Alpaca options snapshot dict into an OptionsQuote."""
        # Alpaca snapshot structure:
        # {
        #   "latestQuote": {"ap": ask, "bp": bid, ...},
        #   "latestTrade": {"p": last, ...},
        #   "greeks": {"delta": ..., "gamma": ..., "theta": ..., "vega": ..., "rho": ...},
        #   "impliedVolatility": 0.35,
        # }

        latest_quote = snap.get("latestQuote", {})
        latest_trade = snap.get("latestTrade", {})
        greeks_raw = snap.get("greeks", {})

        bid = latest_quote.get("bp") or latest_quote.get("bid_price")
        ask = latest_quote.get("ap") or latest_quote.get("ask_price")
        last = latest_trade.get("p") or latest_trade.get("price")
        mid = ((bid or 0) + (ask or 0)) / 2 if bid is not None and ask is not None else None

        greeks = OptionsGreeks(
            delta=greeks_raw.get("delta"),
            gamma=greeks_raw.get("gamma"),
            theta=greeks_raw.get("theta"),
            vega=greeks_raw.get("vega"),
            rho=greeks_raw.get("rho"),
            implied_volatility=snap.get("impliedVolatility"),
        )

        # Parse timestamp
        updated_at: Optional[datetime] = None
        ts_str = latest_quote.get("t") or latest_trade.get("t")
        if ts_str:
            try:
                updated_at = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except Exception:
                pass

        return OptionsQuote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=last,
            mid=mid,
            open_interest=snap.get("openInterest"),
            volume=snap.get("dailyBar", {}).get("v"),
            greeks=greeks,
            updated_at=updated_at,
        )

    # ── Convenience single-quote ─────────────────────────────────────

    async def get_option_quote(self, contract_symbol: str) -> Optional[OptionsQuote]:
        """Fetch a live quote for a single contract.

        Args:
            contract_symbol: OCC-format symbol, e.g. ``"AAPL260418C00255000"``.

        Returns:
            :class:`OptionsQuote` if data is available, else ``None``.
        """
        results = await self.fetch_snapshot([contract_symbol])
        return results.get(contract_symbol)

    # ── Chain + quote helper ─────────────────────────────────────────

    async def fetch_chain_with_quotes(
        self,
        underlying: str,
        option_type: Optional[OptionType] = None,
        min_strike: Optional[float] = None,
        max_strike: Optional[float] = None,
        min_expiry: Optional[date] = None,
        max_expiry: Optional[date] = None,
        limit: int = 100,
    ) -> list[tuple[OptionsContract, Optional[OptionsQuote]]]:
        """Fetch the options chain and enrich each contract with a live quote.

        Returns a list of (contract, quote) tuples.  ``quote`` will be ``None``
        if no market data snapshot was returned for that symbol.
        """
        contracts = await self.fetch_chain(
            underlying=underlying,
            option_type=option_type,
            min_strike=min_strike,
            max_strike=max_strike,
            min_expiry=min_expiry,
            max_expiry=max_expiry,
            limit=limit,
        )

        if not contracts:
            return []

        symbols = [c.symbol for c in contracts]
        quotes = await self.fetch_snapshot(symbols)

        return [(c, quotes.get(c.symbol)) for c in contracts]
