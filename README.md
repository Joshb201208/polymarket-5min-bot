# Stock Trading Agent

Autonomous US equities and defined-risk options trading bot built for a lightweight Linux VPS (Python 3.12, 1 vCPU, 1 GB RAM). It combines scheduled deep research, intraday material-news reactions, strict broker-reconciled state, and multi-layered risk and execution guardrails — defaulting safely to Alpaca paper trading.

For provider key rotation, server `.env` cleanup, and repository hardening steps, see [`docs/revamp/OWNER_CHECKLIST.md`](docs/revamp/OWNER_CHECKLIST.md).

---

## Safety & Operating Principles

- **Fail-closed Paper/Live lock:** Startup validates `STOCK_AGENT_MODE` against `ALPACA_BASE_URL` (`https://paper-api.alpaca.markets` vs `https://api.alpaca.markets`). Live trading refuses to start unless **all three** of `STOCK_AGENT_MODE=LIVE`, `ALPACA_BASE_URL=https://api.alpaca.markets`, and `STOCK_AGENT_LIVE_CONFIRMED=true` are set. Both equity (`broker.py`) and options (`options_data.py`, `options_executor.py`) derive URLs from `Settings.trading_base_url` and `Settings.market_data_url`.
- **Entry pause switch (`STOCK_TRADING_PAUSED` / `data/PAUSED`):** Setting `STOCK_TRADING_PAUSED=true` or creating an empty `data/PAUSED` (or `data/stock_agent/PAUSED`) file immediately blocks new equity and option entries while keeping protective stop repairs, exits, broker reconciliation, and alerts active.
- **Defined-risk options by default (`OPTIONS_MODE=defined_risk`):** Allows defined-risk vertical debit spreads (`order_class="mleg"`), protective puts, and covered calls backed by settled unencumbered shares; rejects naked short options and cash-secured puts unless explicitly configured otherwise, and closes expiring contracts ahead of expiration (`T-2` / `T-1` DTE).
- **Protected execution & broker reconciliation:** Equity entries verify market hours, quote freshness, spread width, buying power, wash-trade conflicts, and protective stop coverage, reconciling local state (`ledger.py`, `reconcile.py`) against Alpaca fills and positions before trading.
- **Zero-leak logging:** `stock_agent/logging_setup.py` clamps `httpx`/`httpcore` request logging to `WARNING`, scrubs query-string API keys, Telegram bot tokens, auth headers, and configured secrets across stdout/stderr and rotating log files (`data/stock_agent/logs/stock_agent.log`, 10 MB x 12 backups).

---

## Quick Start (Local Paper Setup)

Requires **Python 3.12+**.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env
# Fill in ALPACA_API_KEY and ALPACA_SECRET_KEY (paper keys) plus data/LLM keys in .env
python -m stock_agent
```

### Running Tests & Lint

Tests run completely offline against high-fidelity in-process fakes (`tests/fakes/`) — never hitting live broker, market-data, or paid LLM endpoints:

```bash
python -m ruff check .
python -m pytest -q
```

---

## Trading & Research Cadence

All session checks use an NYSE market calendar (`stock_agent/market_calendar.py`, `America/New_York`, including holidays and early closes):

- **Startup & pre-open reconciliation:** Validates settings, migrates/loads SQLite/JSON state atomically, reconciles open positions/orders/stops with Alpaca, and repairs missing protective stops.
- **Weekly deep research & midweek refresh:** Builds and refreshes structured symbol theses (`research_pipeline.py`, `analyst.py`, `research_store.py`) across configured LLM providers (`RESEARCH_PROVIDER_ORDER=openai,perplexity,anthropic`) within a weekly spend cap (`RESEARCH_WEEKLY_BUDGET_USD`).
- **Intraday monitoring & live news reaction:** Polls held-symbol news, SEC filings, and price/stop triggers (`news_service.py`, `materiality.py`), re-evaluating material events during market hours without over-spending research budget.
- **Options lifecycle management:** Scans defined-risk opportunities within configured windows and enforces mandatory pre-expiry closes (`options_manager.py`, `options_policy.py`).
- **Reporting & alerts:** Deduplicated severity-routed notifications (`alerts.py`, `discord_alerts.py`, `telegram_alerts.py`), daily mark-to-market equity/benchmark tracking (`performance.py`), and optional external dead-man's-switch pings (`heartbeat.py`).

---

## Configuration

All settings are validated in a single Pydantic v2 model in [`stock_agent/config.py`](stock_agent/config.py) (`Settings` / `load_settings()`), with backward-compatible `Config.X` attribute access and `.env.example` kept drift-tested in CI.

Key environment variables (see [`.env.example`](.env.example) for the full template):

| Variable | Default | Description |
| :--- | :--- | :--- |
| `STOCK_AGENT_MODE` | `PAPER` | `PAPER` or `LIVE` (must match `ALPACA_BASE_URL`). |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` | Alpaca trading endpoint (`paper-api` for `PAPER`, `api` for `LIVE`). |
| `STOCK_AGENT_LIVE_CONFIRMED` | `false` | Required second confirmation (`true`) before `LIVE` mode can start. |
| `STOCK_TRADING_PAUSED` | `false` | Blocks new entries when `true` (or when `data/PAUSED` exists). |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | *(required)* | Alpaca API credentials. |
| `ALPACA_DATA_FEED` | `iex` | Market data feed (`iex` or `sip`). |
| `FINNHUB_API_KEY` / `FMP_API_KEY` | *(optional)* | News, fundamentals, and quote fallback providers. |
| `OPENAI_API_KEY` / `PERPLEXITY_API_KEY` / `ANTHROPIC_API_KEY` | *(optional)* | Research/analyst LLM keys (`RESEARCH_PROVIDER_ORDER` controls priority). |
| `RESEARCH_WEEKLY_BUDGET_USD` | `15.0` | Weekly LLM spend budget with automatic reserve for exit/risk reviews. |
| `OPTIONS_MODE` | `defined_risk` | `off`, `defined_risk`, or `legacy` (`OPTIONS_ENABLED=false` maps to `off`). |
| `DISCORD_BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` | *(optional)* | Outbound alert and summary channels. |
| `HEARTBEAT_PING_URL` | *(optional)* | External dead-man's-switch ping URL. |
| `STOCK_LOG_LEVEL` | `INFO` | Root log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

### Adding a New Setting

1. Add a typed field with its safe default under the owning section on `Settings` in `stock_agent/config.py`.
2. Any secret field ending in `_KEY`, `_SECRET`, `_TOKEN`, or `_PASSWORD` must default to `""` — it is automatically masked in `repr()`, `redacted_summary()`, and `logging_setup.py`.
3. Document user-facing settings in `.env.example` (kept verified by `tests/test_config_settings.py`).

---

## Deployment & CI

- **CI (`.github/workflows/ci.yml`):** Runs Python 3.12 syntax check, `ruff check .`, full `pytest` suite, and Gitleaks secret scanning (`gitleaks/gitleaks-action@v3` with `.gitleaks.toml`) on every pull request and push to `master`.
- **In-place VPS updater (`deploy/update_release.py`):** Triggered by cron via `deploy/auto_update.sh`. Verifies that GitHub Actions CI passed for the exact target `master` commit SHA before fast-forwarding, installing `requirements.txt`, running preflight checks, and restarting `stock-agent.service` (with automatic rollback on failure).
- **Fresh VPS bootstrap (`deploy/setup.sh`):** One-time provisioning script for a clean Ubuntu 24.04 droplet (refuses to overwrite an existing installation).

---

## Package Layout

```text
stock_agent/
├── __main__.py              # Process lock, fail-closed settings validation, logging & startup
├── config.py                # Validated Pydantic Settings, Paper/Live lock & Config wrapper
├── logging_setup.py         # Secret-redacting log filter/formatter, httpx clamp & log rotation
├── scheduler.py             # Market-aware session loop & job orchestration
├── market_calendar.py       # NYSE trading sessions, holidays & early closes
├── broker.py                # Validated Alpaca REST client & decimal/tick helpers
├── execution.py             # Pre-trade gates, order lifecycle & protective stop management
├── ledger.py / state_store.py # Atomic state persistence, trade journal & reconciliation backing
├── reconcile.py             # Startup & periodic broker-vs-ledger reconciliation
├── risk_manager.py          # Exposure, sector/cluster caps, drawdown circuit breakers & stops
├── price_service.py         # Multi-source quotes/bars with freshness & corporate-action checks
├── fundamentals_service.py  # Cached fundamentals with per-endpoint plan/health tracking
├── news_service.py / materiality.py # Deduplicated news, SEC filings & materiality filter
├── research_pipeline.py     # Budgeted multi-provider LLM thesis build & refresh
├── options_policy.py / options_manager.py # Defined-risk options rules, mleg execution & DTE exits
├── alerts.py / heartbeat.py # Deduplicated Discord/Telegram alerts, outbox & external heartbeat
└── performance.py           # Daily equity curve & SPY benchmark reporting
```
