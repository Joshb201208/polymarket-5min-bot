# Polymarket API Reference for Bot Development

## Three API Layers

### 1. Gamma API (Market Discovery)
- Base: `https://gamma-api.polymarket.com`
- No auth needed
- `GET /events` - List events with filtering
- `GET /events/{id}` - Single event
- `GET /markets` - List markets
- `GET /markets/{id}` - Single market
- `GET /markets/slug/{slug}` - Market by slug
- `GET /tags` - Categories
- `GET /public-search` - Search

### 2. CLOB API (Trading)
- Base: `https://clob.polymarket.com`
- Auth: L1 (EIP-712) for credential derivation, L2 (HMAC-SHA256) for trading
- WebSocket: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- `GET /price` - Single token price
- `GET /prices` - Multiple token prices
- `GET /book` - Order book
- `POST /books` - Multiple order books
- `GET /midpoint` - Midpoint price
- `GET /spread` - Spread
- `GET /prices-history` - Historical prices
- Rate limits: 100 req/min public, 60 orders/min trading

### 3. Data API (Positions)
- `GET /positions?user={address}` - Current positions
- `GET /closed-positions?user={address}` - Closed positions
- `GET /activity?user={address}` - Onchain activity
- `GET /value?user={address}` - Total position value
- `GET /oi` - Open interest
- `GET /holders` - Top holders
- `GET /trades` - Trade history

## 5-Minute Crypto Market Specifics
- Slug pattern: `btc-updown-5m-{unix_timestamp}`
- URL pattern: `polymarket.com/event/btc-updown-5m-{timestamp}`
- 288 markets per day per asset (every 5 minutes)
- Assets: BTC, ETH, SOL
- Resolution: Chainlink oracle (BTC/USD, ETH/USD, SOL/USD)
- Settlement: "Up" if end price >= start price, else "Down"
- Fees: crypto feeRate=0.25, exponent=2, max effective rate 1.56% at 50¢
- Maker rebate: 20%

## Gamma API - Finding Crypto 5-Min Markets
Note from Reddit: Gamma API has indexing lag on micro-markets.
The slug follows a predictable pattern — construct the slug and query directly.
Slug format: `btc-updown-5m-{unix_timestamp_of_window_start}`

## Order Types
- GTC (Good-Til-Cancelled) - rests on book
- GTD (Good-Til-Date) - auto-expires
- FOK (Fill-Or-Kill) - all or nothing, immediate
- FAK (Fill-And-Kill) - partial fill, cancel remainder
- Post-only available on GTC/GTD

## Orderbook Structure
```json
{
  "market": "0x...",
  "asset_id": "5211431...",
  "bids": [{"price": "0.48", "size": "1000"}],
  "asks": [{"price": "0.52", "size": "800"}],
  "min_order_size": "5",
  "tick_size": "0.01",
  "neg_risk": false
}
```

## WebSocket Events
- `book` - full orderbook snapshot
- `price_change` - individual price level update
- `last_trade_price` - trade executed
- `best_bid_ask` - top-of-book update (needs custom_feature_enabled)
- `new_market` - market created
- `market_resolved` - market resolved

## Heartbeat (CRITICAL)
Must send heartbeat every 5 seconds, or all open orders cancelled after 10s.
```
POST heartbeat with heartbeat_id (empty string for first request)
```

## Fee Formula (Crypto)
```
fee = C × p × 0.25 × (p × (1 - p))^2
```
Where C = shares traded, p = price
Max effective rate: 1.56% at p=0.50

## Authentication
- Private key → derive L2 credentials (API key, secret, passphrase)
- L2 HMAC-SHA256 for all trading operations
- Orders still require EIP-712 signing with private key

## Python SDK
```
pip install py-clob-client
```

## Key Insight from Research
- 14/20 top profitable wallets are bots
- Edge comes from SPEED, not prediction
- Latency arb: monitor exchange prices, buy mispricing on Polymarket before it updates
- One bot: $300 → $400K in a month via latency arb on 15-min contracts
- $40M extracted by arb traders Apr 2024-Apr 2025
