# NBA Polymarket Betting Agent

Automated NBA betting agent for Polymarket. Scans markets, computes edges using NBA stats, and executes trades via paper or live mode.

## Quick Start

```bash
cp .env.example .env
pip install -r requirements.txt
python -m nba_agent.main
```

## Features

- **Market Discovery**: Scans Polymarket Gamma API for NBA moneylines, spreads, totals, and futures
- **NBA Research**: Pulls standings, game logs, H2H, advanced stats, and injury news
- **Edge Calculation**: Computes fair odds using weighted power ratings
- **Bankroll Management**: Quarter-Kelly sizing with exposure limits and stop-loss
- **Paper & Live Trading**: Simulated or real execution via py-clob-client
- **Telegram Alerts**: Trade notifications, daily/weekly P&L summaries
- **Early Exit System**: Confidence-tiered profit-taking and loss-cutting

## VPS Deployment

```bash
sudo bash deploy/setup.sh
```

## Configuration

All settings via `.env` — see `.env.example` for defaults.

## Architecture

```
nba_agent/
├── main.py              # Async scheduler
├── config.py            # Environment config
├── models.py            # Data models
├── polymarket_scanner.py # Market discovery
├── nba_research.py      # NBA stats engine
├── edge_calculator.py   # Fair odds & edge
├── trading_engine.py    # Order execution
├── bankroll_manager.py  # Position sizing
├── telegram_alerts.py   # Notifications
├── performance_tracker.py # P&L tracking
├── injury_scanner.py    # Injury news
└── utils.py             # Helpers
```
