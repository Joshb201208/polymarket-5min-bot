# Polymarket 5-Minute Crypto Trading Bot

An automated trading bot for Polymarket's 5-minute BTC/ETH/SOL up/down markets.
The bot implements two strategies — latency arbitrage and signal-based — with a
full risk management system, paper trading simulation, and performance monitoring.

---

## Architecture

```
main.py              ← Orchestrator loop (runs every 30s)
│
├── config.py        ← All settings loaded from .env
├── utils.py         ← RSI, MACD, Bollinger Bands, fee math, Telegram
│
├── data_feeds.py    ← Binance WebSocket (BTC/ETH/SOL real-time prices)
├── market_finder.py ← Finds active 5-min Polymarket markets via Gamma API
│
├── strategy.py      ← Strategy engine
│   ├── Strategy 1: Latency Arbitrage (primary)
│   └── Strategy 2: Signal-Based (secondary: RSI + MACD + BB)
│
├── executor.py      ← CLOB API order placement + heartbeat
├── risk_manager.py  ← Kelly sizing, loss limits, circuit breakers
├── monitor.py       ← CSV trade log, JSON stats, Telegram alerts
└── paper_trader.py  ← Paper trading simulation (no real orders)
```

### Data Flow

```
Binance WebSocket
      │
      ▼
  PriceFeed  ──── get_momentum(), get_rsi(), etc.
      │
      ▼
StrategyEngine  ──── evaluate(market) → TradingSignal
      │
      ▼
RiskManager  ──── can_trade()? → calculate_position_size()
      │
      ├─ paper mode ──▶  PaperTrader.simulate_order()
      └─ live  mode ──▶  OrderExecutor.place_order()
                                │
                                ▼
                        PerformanceMonitor.log_trade()
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your settings
```

For **paper mode**, only `TRADING_MODE=paper` is required.  
For **live mode**, you need Polymarket credentials (private key, API key, etc.).

### 3. Polymarket credentials (live mode only)

Generate API credentials with the py-clob-client:

```python
from py_clob_client.client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    key="YOUR_PRIVATE_KEY",
    chain_id=137,
)
creds = client.create_or_derive_api_creds()
print(creds)
```

Add the resulting `api_key`, `api_secret`, `api_passphrase` to your `.env`.

---

## Running the Bot

### Paper mode (safe — no real money)

```bash
TRADING_MODE=paper python main.py
```

Or set `TRADING_MODE=paper` in `.env` and run:

```bash
python main.py
```

### Live mode

```bash
TRADING_MODE=live python main.py
```

The bot will run until you press `Ctrl+C` or it hits a risk limit.

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `ASSETS` | `BTC,ETH,SOL` | Comma-separated list |
| `PRIMARY_STRATEGY` | `latency_arb` | `latency_arb` or `signal_based` |
| `MAX_POSITION_PCT` | `0.03` | Max 3% of balance per trade |
| `DAILY_LOSS_LIMIT_PCT` | `0.10` | Stop at 10% daily loss |
| `MAX_DRAWDOWN_PCT` | `0.20` | Stop at 20% drawdown |
| `MIN_EDGE_THRESHOLD` | `0.02` | Minimum net edge to trade |
| `KELLY_FRACTION` | `0.50` | Half-Kelly position sizing |
| `PAPER_BALANCE` | `500` | Starting virtual balance |
| `LATENCY_ARB_THRESHOLD` | `0.001` | 0.1% price move threshold |
| `LATENCY_ARB_LOOKBACK_SECONDS` | `30` | Momentum lookback window |
| `SIGNAL_CONFIDENCE_THRESHOLD` | `0.60` | Min confidence for signal-based trades |

---

## Output Files

| File | Contents |
|---|---|
| `polymarket_bot.log` | Full application log |
| `trades.csv` | Every resolved trade (for analysis) |
| `stats.json` | Running performance stats |
| `paper_state.json` | Paper trading state (open orders, balance) |

---

## Strategies

### Strategy 1: Latency Arbitrage (Primary)

Exploits the delay between Binance price movement and Polymarket's
orderbook repricing.

**Signal trigger:**
- BTC moves +0.1% on Binance in the last 30 seconds
- Polymarket "Up" token is still priced at ≤ 0.50
- Edge = exchange_implied_prob − polymarket_mid − fees

**Why it works:**  
Polymarket's 5-minute markets are priced by market makers who can't update
instantly. A fast bot monitoring Binance can detect price momentum before the
Polymarket book reprices, creating a profitable edge.

### Strategy 2: Signal-Based (Secondary)

Uses technical indicators on 1-minute price data:
- RSI < 30 + positive momentum → BUY UP
- RSI > 70 + negative momentum → BUY DOWN
- MACD bullish/bearish crossover
- Bollinger Band bounce / rejection

---

## Risk Management

- **Half-Kelly sizing**: Conservative position sizing using the Kelly criterion
  with a 0.5 multiplier.
- **Daily loss limit**: Bot stops trading if daily P&L drops below −10%.
- **Max drawdown**: Bot stops if drawdown from peak exceeds 20%.
- **Circuit breaker**: After 5 consecutive losses, trading pauses for 15 minutes.
- **Position cap**: Maximum 3% of balance per trade.
- **Concurrent positions**: Maximum 3 open positions.

---

## Fees

Polymarket charges a variable fee for crypto markets:

```
fee = C × p × 0.25 × (p × (1 − p))²
```

Maximum effective rate: **1.56%** at p = 0.50.
All fee calculations are included in edge computation before any trade is placed.

---

## RISK WARNING

**Trading prediction markets involves substantial financial risk.**
- Never trade money you cannot afford to lose.
- Past performance of any strategy does not guarantee future results.
- Run in paper mode first to understand bot behavior.
- The latency arb strategy is competitive — other bots also trade these markets.
- Polymarket's 5-minute markets are highly efficient; edges are small and fleeting.
- This software is provided AS IS with no warranty of any kind.

---

## Telegram Alerts

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` to receive:
- Startup notification
- Trade alerts for significant wins/losses (> $0.50)
- Circuit breaker / emergency stop events

Create a bot via [@BotFather](https://t.me/BotFather) on Telegram.
