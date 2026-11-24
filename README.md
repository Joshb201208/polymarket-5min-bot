# Stock Trading Agent

Autonomous US equities and defined-risk options trading bot built for a lightweight Linux VPS (Python 3.12, 1 vCPU, 1 GB RAM). It combines scheduled deep research, intraday material-news reactions, strict broker-reconciled state, overnight Reg T margin controls, and multi-layered risk and execution guardrails — defaulting safely to Alpaca paper trading.

For provider key rotation, server `.env` cleanup (`scripts/clean_server_env.py`), and repository hardening steps, see [`docs/revamp/OWNER_CHECKLIST.md`](docs/revamp/OWNER_CHECKLIST.md).

---

## Safety & Operating Principles

- **Fail-closed Paper/Live lock:** Startup validates `STOCK_AGENT_MODE` against `ALPACA_BASE_URL` (`https://paper-api.alpaca.markets` vs `https://api.alpaca.markets`). Live trading refuses to start unless **all three** of `STOCK_AGENT_MODE=LIVE`, `ALPACA_BASE_URL=https://api.alpaca.markets`, and `STOCK_AGENT_LIVE_CONFIRMED=true` are set. Both equity (`broker.py`) and options (`options_manager.py`) derive URLs from `Settings.trading_base_url` and `Settings.market_data_url`.
- **Entry pause switch (`STOCK_TRADING_PAUSED` / `data/PAUSED`):** Setting `STOCK_TRADING_PAUSED=true` or creating an empty `data/PAUSED` (or `data/stock_agent/PAUSED`) file immediately blocks new equity and option entries while keeping protective stop repairs, exits, broker reconciliation, and alerts active.
- **Overnight Reg T margin with excess-margin buffers (`MARGIN_ENABLED=true`):** Long-only equities (no short stock; `no_shorting` enforced) borrow on an overnight Reg T basis only (`regt_buying_power`, gross long cap `MAX_GROSS_EXPOSURE_PCT=2.0`, never intraday 4x day-trading buying power). New buys are refused if projected maintenance excess `(equity - maintenance_margin) / equity` would fall below `MARGIN_MIN_EXCESS_PCT` (`0.25`); if excess drops below `MARGIN_TRIM_EXCESS_PCT` (`0.15`), a critical alert fires and positions are trimmed back to `>= 25%` excess before the broker ever needs to liquidate. At the `15%` drawdown tier, new borrowing is halted alongside half-sizing. Setting `MARGIN_ENABLED=false` restores `1.0x` cash-only rules. Legacy `MAX_TOTAL_EXPOSURE_PCT` is retired and ignored with a startup notice.
- **Defined-risk options by default (`OPTIONS_MODE=defined_risk`):** Valid modes are `off`, `close_only`, and `defined_risk` (legacy `OPTIONS_ENABLED=false` maps to `off`). Long option debits cannot be bought on margin (funded from `options_buying_power` / non-marginable buying power). `defined_risk` allows debit structures (`order_class="mleg"` vertical spreads, long calls, long puts) while rejecting naked short options; `close_only` never opens new contracts while continuing to manage, repair, and exit existing option positions ahead of expiration (`T-2` / `T-1` DTE).
- **Owner watch list (`stock_agent/owner_focus.json`):** Optional list of up to 25 time-bounded (`added` .. `expires`, max 16 KiB) `symbol` or `theme` entries loaded via `stock_agent/research_focus.py`. Entries boost news-polling and research-queue **priority only — never eligibility to trade**, position sizing, or risk/execution gates. Expired or malformed entries are ignored cleanly.
- **Protected execution & broker reconciliation:** Equity entries verify market hours, quote freshness, spread width, Reg T buying power, wash-trade conflicts, and protective stop coverage, reconciling local state (`ledger.py`, `reconcile.py`) against Alpaca fills and positions before trading.
- **Zero-leak logging:** `stock_agent/logging_setup.py` clamps `httpx`/`httpcore` request logging to `WARNING` and scrubs query-string API keys, Telegram bot tokens, auth headers, and configured secrets across stdout/stderr and rotating log files (`data/stock_agent/logs/stock_agent.log`, 10 MB x 12 backups).

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

- **Startup & pre-open reconciliation:** Validates settings, migrates/loads state atomically (`state_boot.py`, `state_store.py`), reconciles open equity and option positions/orders/stops with Alpaca (`reconcile.py`, `options_book.py`), and repairs missing protective stops.
- **First-start catch-up, weekly deep research & midweek refresh:** Runs research immediately on first start whenever no fresh thesis set exists, plus scheduled weekly/midweek refreshes (`research_pipeline.py`, `research_sources.py`, `analyst.py`, `research_store.py`) across configured LLM providers (`RESEARCH_PROVIDER_ORDER=perplexity,openai,anthropic`) within `RESEARCH_WEEKLY_BUDGET_USD`, prioritizing held positions and active `owner_focus.json` items without bypassing screening or risk gates.
- **Intraday monitoring & live news reaction:** Polls held-symbol, focus-symbol, and universe news plus SEC filings and price/stop/margin triggers (`news_service.py`, `materiality.py`, `price_service.py`), re-evaluating material events during market hours without over-spending research budget.
- **Options lifecycle management:** Evaluates defined-risk structures within configured windows and enforces mandatory pre-expiry closes and leg reconciliation (`options_manager.py`, `options_policy.py`, `options_book.py`, `options_settings.py`).
- **Reporting, entry funnel & alerts:** Deduplicated severity-routed notifications (`alerts.py`, `discord_alerts.py`, `telegram_alerts.py`), daily summary entry funnel (`screened -> thesis written -> passed conviction -> passed risk -> ordered -> filled` plus active entry-lock explanations), leverage/margin-excess reporting with returns shown both broker-reported and net of overnight debit interest (`reporting.py`, `performance.py`, `benchmark.py`), and optional external dead-man's-switch pings (`heartbeat.py`).

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
| `MARGIN_ENABLED` | `true` | Enables Reg T overnight margin borrowing (`false` restores `1.0x` cash-only rules). |
| `MAX_GROSS_EXPOSURE_PCT` | `2.0` | Max gross long stock exposure as a multiple of equity (`<= 2.0`; effective `1.0` when `MARGIN_ENABLED=false`). |
| `MARGIN_MIN_EXCESS_PCT` | `0.25` | Minimum post-trade maintenance excess `(equity - maintenance_margin) / equity` required for new buys. |
| `MARGIN_TRIM_EXCESS_PCT` | `0.15` | Maintenance-excess threshold triggering critical alert and automatic trim back to `>= 0.25`. |
| `MARGIN_INTEREST_RATE_PCT` | `6.5` | Annualized margin debit interest rate (percentage points) used for net-of-interest reporting when broker interest activities are absent. |
| `FINNHUB_API_KEY` / `FMP_API_KEY` | *(optional)* | News, fundamentals (`fundamentals_service.py`), and quote fallback providers. |
| `SEC_USER_AGENT` | *(optional)* | User-Agent header enabling SEC EDGAR filing checks. |
| `OPENAI_API_KEY` / `PERPLEXITY_API_KEY` / `ANTHROPIC_API_KEY` | *(optional)* | Research/analyst LLM keys (`RESEARCH_PROVIDER_ORDER` controls priority). |
| `RESEARCH_WEEKLY_BUDGET_USD` | `15.0` | Weekly LLM spend budget with automatic reserve for exit/risk reviews. |
| `OPTIONS_MODE` | `defined_risk` | `off`, `close_only`, or `defined_risk` (`OPTIONS_ENABLED=false` maps to `off`). |
| `DISCORD_BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` | *(optional)* | Outbound alert and summary channels. |
| `HEARTBEAT_PING_URL` | *(optional)* | External dead-man's-switch ping URL. |
| `STOCK_LOG_LEVEL` | `INFO` | Root log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

> **Retired setting note:** Legacy `MAX_TOTAL_EXPOSURE_PCT` is retired and ignored at startup (logging only the variable name once) so old server `.env` files set to `1.00` do not override `MAX_GROSS_EXPOSURE_PCT`. Use `python3 scripts/clean_server_env.py /path/to/.env` (`--dry-run` by default, `--apply` to write a `0600` backup and clean) to remove retired server `.env` entries safely.

### Owner Watch List (`stock_agent/owner_focus.json`)

Edit `stock_agent/owner_focus.json` to spotlight specific tickers or macro/sector themes for research:

```json
[
  {
    "symbol": "NVDA",
    "note": "Focus research on corroborated delivery and margin disclosures.",
    "added": "2026-11-23",
    "expires": "2026-12-04"
  }
]
```

Each entry specifies either `"symbol"` or `"theme"` (never both), a `"note"` (up to 300 chars), and ISO `"added"` / `"expires"` dates. Active items increase research and news-polling **priority only** — every trade still requires corroborated thesis output and must pass all liquidity, exposure, cluster, margin, and execution gates.

### Adding a New Setting

1. Add a typed field with its safe default under the owning section on `Settings` in `stock_agent/config.py`.
2. Any secret field ending in `_KEY`, `_SECRET`, `_TOKEN`, or `_PASSWORD` must default to `""` — it is automatically masked in `repr()`, `redacted_summary()`, and `logging_setup.py`.
3. Document user-facing settings in `.env.example` (kept verified by `tests/test_config_settings.py`).

---

## Deployment & CI

- **CI (`.github/workflows/ci.yml`):** Runs Python 3.12 syntax check, `ruff check .`, full `pytest` suite, and Gitleaks secret scanning (`gitleaks/gitleaks-action@v3` with `.gitleaks.toml`) on every pull request and push to `master`.
- **In-place VPS updater (`deploy/update_release.py`):** Cron verifies CI for the exact target `master` commit and confirms the market is closed with at least 30 minutes before the next open. It updates the checkout and verifies the restarted service's readiness, rolling back if an update to a running service fails.
- **Recovery after a failed update:** If systemd shows the bot as `failed`, pushing a new fix or revert to `master` lets cron attempt recovery at the next closed-market check after CI passes. Cron will not keep retrying the failed SHA. Stopping the bot on purpose with `systemctl stop` leaves it `inactive`; cron does not start an inactive service.
- **Fresh VPS bootstrap (`deploy/setup.sh`):** One-time provisioning script for a clean Ubuntu 24.04 droplet (refuses to overwrite an existing installation).

---

## Package Layout

```text
stock_agent/
├── __main__.py                    # Process lock, fail-closed settings validation, logging & startup
├── config.py                      # Validated Pydantic Settings, Paper/Live lock, margin config & Config wrapper
├── logging_setup.py               # Secret-redacting log filter/formatter, httpx clamp & log rotation
├── http_safety.py                 # Bounded HTTP response/JSON readers & transport safety helpers
├── scheduler.py                   # Market-aware session loop & job orchestration
├── market_calendar.py             # NYSE trading sessions, holidays & early closes
├── broker.py                      # Validated Alpaca REST client & decimal/tick helpers
├── asset_registry.py              # Tradability, fractionability & canonical sector metadata
├── execution.py                   # Pre-trade gates, order lifecycle & protective stop management
├── state_boot.py / state_store.py # Startup v0 migration, SQLite KV store & atomic state persistence
├── ledger.py / journal.py         # Equity position ledger, lots, order intents & JSONL trade journal
├── reconcile.py                   # Startup & periodic broker-vs-ledger reconciliation
├── risk_manager.py                # Reg T margin buffers, exposure/sector/cluster caps, drawdown & stops
├── price_service.py               # Multi-source quotes/bars with freshness & corporate-action checks
├── fundamentals_service.py        # Cached fundamentals with per-endpoint plan/health tracking
├── news_service.py / materiality.py # Deduplicated news, SEC filings & materiality filter
├── macro_monitor.py / screener.py # Macro regime checks & candidate universe screening
├── owner_focus.json / research_focus.py # Time-bounded owner watch list (priority only, never eligibility)
├── llm_client.py / analyst.py     # Multi-provider LLM router, structured schemas & thesis synthesis
├── research_pipeline.py / research_sources.py # Budgeted thesis pipeline, gates (`research_gate.py`) & store (`research_store.py`)
├── options_settings.py / options_policy.py # `OPTIONS_MODE` (`off` | `close_only` | `defined_risk`) & structure rules
├── options_book.py / options_manager.py # Reconciled options book, atomic `mleg` execution & DTE exits
├── health.py / runtime_status.py  # Provider circuit-breaker health registry & runtime status snapshots
├── alerts.py / heartbeat.py       # Deduplicated Discord/Telegram alerts, outbox & external heartbeat
└── reporting.py / performance.py / benchmark.py # Entry-funnel reports, margin-adjusted returns & QQQ/SPY benchmarks
```
