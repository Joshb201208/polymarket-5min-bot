# Stock Trading Agent

Autonomous equities research + paper/live trading agent. Runs a weekly deep-research cycle and a midweek update cycle, combines a multi-source screener (TipRanks Smart Score, Finnhub news/sentiment/analyst trends, macro monitor) with an LLM-driven thesis/analyst layer, and sizes positions with a conviction-based risk manager.

## Quick Start

```bash
cp .env.example .env     # fill in API keys
pip install -r requirements.txt
python -m stock_agent
```

## Cadence

- **Sunday (WEEKLY_ANALYSIS_DAY=6)** — full discovery + thesis build + portfolio rebalance
- **Wednesday (MIDWEEK_ANALYSIS_DAY=2)** — midweek check-in, add-to-position, risk review
- Paper mode is the default; live mode flips a single config flag.

## Inputs

- **TipRanks Smart Score** — API screener, conviction boost for SS 8–10
- **Finnhub** — company news, news/social sentiment, analyst recommendation trends, insider transactions
- **Macro monitor** — rates/VIX/USD regime checks
- **Options data** — IV, skew, flow for the options engine
- **Perplexity + Anthropic** — thesis research and analyst reasoning

## Outputs

- Telegram trade alerts + weekly/daily summaries
- Discord channel posts per trade
- Position ledger persisted under `data/`

## VPS Deployment

```bash
sudo bash deploy/setup.sh
```

Installs the `stock-agent` systemd unit and a 10-minute auto-update cron that pulls new commits on `master` and restarts the service.

## Configuration

All knobs live in `stock_agent/config.py` and `.env` — see `.env.example`.

Key flags:
- `MIN_CONVICTION` — floor for taking a position (default 7)
- `TIPRANKS_ENABLED`, `TIPRANKS_MIN_SMART_SCORE`, `TIPRANKS_UNIVERSE_LIMIT`
- `POSITION_SIZE_PCT_BY_CONVICTION` — 5–10% sliding scale
- Sector caps and hard stop-loss logic live in `risk_manager.py` (do not mutate without a plan)

## Architecture

```
stock_agent/
├── __main__.py          # Entry point
├── scheduler.py         # Weekly/midweek loop
├── screener.py          # Universe construction
├── tipranks_client.py   # TipRanks Smart Score API
├── news_feed.py         # Finnhub company news
├── sentiment.py         # Finnhub news + social sentiment
├── analyst_trends.py    # Finnhub analyst recs + insider txns
├── macro_monitor.py     # Macro regime checks
├── data_feed.py         # Price/fundamentals
├── analyst.py           # LLM thesis + analysis
├── portfolio.py         # Position ledger
├── risk_manager.py      # Stops, sector caps, sizing
├── executor.py          # Equity order execution
├── options_engine.py    # Options thesis
├── options_executor.py  # Options order execution
├── options_portfolio.py # Options ledger
├── options_data.py      # Options chain data
├── telegram_alerts.py   # Notifications
├── discord_alerts.py    # Channel posts
├── config.py            # Env + tunables
└── models.py            # Data models
```
