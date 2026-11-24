"""Tests for validated runtime settings, safety locks, and Config compatibility."""

from __future__ import annotations

from pathlib import Path

import pytest

from stock_agent.config import (
    DEFAULT_ALPACA_DATA_URL,
    LIVE_ALPACA_BASE_URL,
    PAPER_ALPACA_BASE_URL,
    Config,
    Settings,
    SettingsError,
    load_settings,
)

# Exact setting NAMES from the server snapshot's env-summary.txt (synthetic test values only).
SERVER_ENV_SUMMARY_SYNTHETIC: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "TRADING_MODE": "live",
    "POLYMARKET_API_KEY": "",
    "POLYMARKET_API_SECRET": "",
    "POLYMARKET_API_PASSPHRASE": "",
    "PRIVATE_KEY": "0x" + "a" * 64,
    "FUNDER_ADDRESS": "0x" + "b" * 40,
    "STARTING_BANKROLL": "100.00",
    "MAX_BET_PCT": "0.10",
    "MAX_TOTAL_EXPOSURE_PCT": "1.00",
    "MAX_GAME_EXPOSURE_PCT": "0.15",
    "SCAN_INTERVAL": "180",
    "EXIT_CHECK_INTERVAL": "60",
    "PRE_TIPOFF_MINUTES": "30",
    "MIN_GAME_EDGE": "0.06",
    "MIN_FUTURES_EDGE": "0.08",
    "ODDS_API_KEY": "unused-odds-key",
    "BALLDONTLIE_API_KEY": "unused-bdl-key",
    "PROXY_HOST": "proxy.example.invalid",
    "PROXY_PORT": "10001",
    "PROXY_USER": "unused-user",
    "PROXY_PASS": "unused-pass",
    "NBA_SEASON": "2026",
    "LOG_LEVEL": "INFO",
    "NHL_TRADING_MODE": "paper",
    "TWITTER_BEARER_TOKEN": "unused-twitter-token",
    "ALPACA_API_KEY": "test-alpaca-key",
    "ALPACA_SECRET_KEY": "test-alpaca-secret",
    "PERPLEXITY_API_KEY": "test-perplexity-key",
    "OPENAI_API_KEY": "test-openai-key",
    "FMP_API_KEY": "test-fmp-key",
    "FINNHUB_API_KEY": "test-finnhub-key",
    "STOCK_AGENT_MODE": "PAPER",
    "DISCORD_BOT_TOKEN": "test-discord-token",
    "TIPRANKS_EMAIL": "unused@example.invalid",
    "TIPRANKS_PASSWORD": "unused-tipranks-pass",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "EVENTS_STARTING_BANKROLL": "100.00",
}


def test_default_settings_are_paper_and_safe() -> None:
    s = load_settings({})
    assert s.MODE == "PAPER"
    assert s.ALPACA_BASE_URL == PAPER_ALPACA_BASE_URL
    assert s.trading_base_url == PAPER_ALPACA_BASE_URL
    assert s.market_data_url == DEFAULT_ALPACA_DATA_URL
    assert s.ALPACA_DATA_FEED == "iex"
    assert s.STOCK_AGENT_LIVE_CONFIRMED is False
    assert s.LIVE_TRADING_CONFIRMED is False
    assert s.OPTIONS_MODE == "defined_risk"
    assert s.OPTIONS_ENABLED is True
    assert s.STOCK_TRADING_PAUSED is False
    assert s.RESEARCH_PROVIDER_ORDER == ("perplexity", "openai", "anthropic")
    assert s.RESEARCH_WEEKLY_BUDGET_USD == 15.0
    assert s.RESEARCH_EXIT_RESERVE_PCT == 20
    assert isinstance(s.RESEARCH_EXIT_RESERVE_PCT, int)
    assert s.RESEARCH_MAX_MODEL_EXITS_PER_SESSION == 4


def test_starts_cleanly_with_only_todays_server_env_names() -> None:
    s = load_settings(SERVER_ENV_SUMMARY_SYNTHETIC, require_runtime_secrets=True)
    assert s.MODE == "PAPER"
    assert s.trading_base_url == PAPER_ALPACA_BASE_URL
    assert s.OPTIONS_MODE == "defined_risk"
    assert s.MAX_GROSS_EXPOSURE_PCT == 2.00
    assert s.OPENAI_API_KEY == "test-openai-key"
    assert s.ANTHROPIC_API_KEY == "test-anthropic-key"
    assert not hasattr(s, "PRIVATE_KEY")
    summary = s.redacted_summary()
    assert summary["providers_configured"]["alpaca"] is True
    assert summary["providers_configured"]["openai"] is True
    assert summary["providers_configured"]["anthropic"] is True
    assert summary["providers_configured"]["discord"] is True
    assert summary["providers_configured"]["telegram"] is False


def test_no_secret_defaults_exist_in_code_or_repr() -> None:
    defaults = Settings()
    secret_fields = [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "PERPLEXITY_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "FMP_API_KEY",
        "FINNHUB_API_KEY",
        "TIPRANKS_API_KEY",
        "TIPRANKS_API_TOKEN",
        "TIPRANKS_EMAIL",
        "TIPRANKS_PASSWORD",
        "DISCORD_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    for field_name in secret_fields:
        assert getattr(defaults, field_name) == "", f"{field_name} has a non-empty default"

    populated = load_settings(SERVER_ENV_SUMMARY_SYNTHETIC)
    secret_vals = populated.secret_values()
    assert "test-alpaca-secret" in secret_vals
    assert "test-fmp-key" in secret_vals
    for rendered in (repr(populated), str(populated), str(populated.redacted_summary())):
        for secret in secret_vals:
            assert secret not in rendered


def test_paper_mode_rejects_live_broker_url() -> None:
    with pytest.raises(SettingsError) as exc_info:
        load_settings({"STOCK_AGENT_MODE": "PAPER", "ALPACA_BASE_URL": LIVE_ALPACA_BASE_URL})
    msg = str(exc_info.value)
    assert "STOCK_AGENT_MODE" in msg
    assert "ALPACA_BASE_URL" in msg

    with pytest.raises(SettingsError) as exc_info2:
        load_settings({"STOCK_AGENT_MODE": "PAPER", "STOCK_AGENT_LIVE_CONFIRMED": "true"})
    assert "STOCK_AGENT_LIVE_CONFIRMED" in str(exc_info2.value)


def test_live_mode_rejects_paper_broker_url() -> None:
    with pytest.raises(SettingsError) as exc_info:
        load_settings({"STOCK_AGENT_MODE": "LIVE", "STOCK_AGENT_LIVE_CONFIRMED": "true"})
    msg = str(exc_info.value)
    assert "STOCK_AGENT_MODE" in msg
    assert "ALPACA_BASE_URL" in msg

    with pytest.raises(SettingsError) as exc_info2:
        load_settings({
            "STOCK_AGENT_MODE": "LIVE",
            "ALPACA_BASE_URL": PAPER_ALPACA_BASE_URL,
            "STOCK_AGENT_LIVE_CONFIRMED": "true",
        })
    assert "ALPACA_BASE_URL" in str(exc_info2.value)


def test_live_mode_requires_explicit_second_confirmation() -> None:
    with pytest.raises(SettingsError) as exc_info:
        load_settings({"STOCK_AGENT_MODE": "LIVE", "ALPACA_BASE_URL": LIVE_ALPACA_BASE_URL})
    assert "STOCK_AGENT_LIVE_CONFIRMED" in str(exc_info.value)


def test_live_mode_succeeds_only_when_mode_url_and_confirmation_all_agree() -> None:
    s = load_settings({
        "STOCK_AGENT_MODE": "live",
        "ALPACA_BASE_URL": f"{LIVE_ALPACA_BASE_URL}/",
        "STOCK_AGENT_LIVE_CONFIRMED": "true",
    })
    assert s.MODE == "LIVE"
    assert s.trading_base_url == LIVE_ALPACA_BASE_URL
    assert s.LIVE_TRADING_CONFIRMED is True


def test_runtime_secret_check_fails_fast_naming_missing_keys() -> None:
    with pytest.raises(SettingsError) as exc_info:
        load_settings({}, require_runtime_secrets=True)
    msg = str(exc_info.value)
    assert "ALPACA_API_KEY" in msg
    assert "ALPACA_SECRET_KEY" in msg


@pytest.mark.parametrize(
    ("env", "expected_name"),
    [
        ({"MAX_POSITION_PCT": "15"}, "MAX_POSITION_PCT"),
        ({"MAX_POSITION_PCT": "15%"}, "MAX_POSITION_PCT"),
        ({"MAX_GROSS_EXPOSURE_PCT": "2.01"}, "MAX_GROSS_EXPOSURE_PCT"),
        ({"MARGIN_ENABLED": "maybe"}, "MARGIN_ENABLED"),
        ({"MARGIN_TRIM_EXCESS_PCT": ".26"}, "MARGIN_TRIM_EXCESS_PCT"),
        ({"OPTIONS_ENABLED": "maybe"}, "OPTIONS_ENABLED"),
        ({"STOCK_TRADING_PAUSED": "sometimes"}, "STOCK_TRADING_PAUSED"),
        ({"STOCK_AGENT_MODE": "SIM"}, "STOCK_AGENT_MODE"),
        ({"OPTIONS_MODE": "naked_short"}, "OPTIONS_MODE"),
        ({"STOCK_LOG_LEVEL": "VERBOSE"}, "STOCK_LOG_LEVEL"),
        ({"RESEARCH_PROVIDER_ORDER": "openai,grok"}, "RESEARCH_PROVIDER_ORDER"),
    ],
)
def test_invalid_percentage_or_bool_fails_fast_with_setting_name(
    env: dict[str, str], expected_name: str
) -> None:
    with pytest.raises(SettingsError) as exc_info:
        load_settings(env)
    assert expected_name in str(exc_info.value)


def test_retired_gross_cap_ignored_and_warned_once_at_startup(caplog):
    env = {"MAX_TOTAL_EXPOSURE_PCT": "retired-value", "MAX_GROSS_EXPOSURE_PCT": "1.75"}
    load_settings(env)  # Import/utility loads do not emit another notice.
    s = load_settings(env, warn_retired=True)
    assert s.MARGIN_ENABLED and s.effective_max_gross_exposure_pct == 1.75
    assert not hasattr(s, "MAX_TOTAL_EXPOSURE")
    notices = [r.message for r in caplog.records if "MAX_TOTAL_EXPOSURE_PCT" in r.message]
    (notice,) = notices
    assert "retired-value" not in notice
    assert load_settings(env | {"MARGIN_ENABLED": "false"}).effective_max_gross_exposure_pct == 1.00


def test_options_mode_precedence_and_legacy_options_enabled_mapping() -> None:
    assert load_settings({"OPTIONS_ENABLED": "false"}).OPTIONS_MODE == "off"
    assert load_settings({"OPTIONS_ENABLED": "false"}).OPTIONS_ENABLED is False
    assert load_settings({"OPTIONS_ENABLED": "0"}).OPTIONS_MODE == "off"
    assert load_settings({"OPTIONS_ENABLED": "true"}).OPTIONS_MODE == "defined_risk"
    assert load_settings({
        "OPTIONS_MODE": "close_only",
        "OPTIONS_ENABLED": "true",
    }).OPTIONS_MODE == "close_only"
    assert load_settings({
        "OPTIONS_MODE": "defined_risk",
        "OPTIONS_ENABLED": "false",
    }).OPTIONS_MODE == "defined_risk"


def test_trading_paused_switch_env_and_pause_file(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    bot_data = data_root / "stock_agent"
    bot_data.mkdir(parents=True)

    s = load_settings({"STOCK_DATA_DIR": str(bot_data)})
    assert s.is_trading_paused() is False

    pause_in_bot_dir = bot_data / "PAUSED"
    pause_in_bot_dir.write_text("paused\n")
    assert s.is_trading_paused() is True
    pause_in_bot_dir.unlink()
    assert s.is_trading_paused() is False

    pause_in_parent_data = data_root / "PAUSED"
    pause_in_parent_data.write_text("paused\n")
    assert s.is_trading_paused() is True
    pause_in_parent_data.unlink()

    s_env = load_settings({"STOCK_DATA_DIR": str(bot_data), "STOCK_TRADING_PAUSED": "true"})
    assert s_env.STOCK_TRADING_PAUSED is True
    assert s_env.is_trading_paused() is True


def test_config_class_and_instance_backward_compatibility_with_monkeypatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert Config.trading_base_url == PAPER_ALPACA_BASE_URL
    assert Config.market_data_url == DEFAULT_ALPACA_DATA_URL

    monkeypatch.setattr(Config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(Config, "ALPACA_API_KEY", "patched-key")
    cfg = Config()
    assert cfg.DATA_DIR == tmp_path
    assert cfg.ALPACA_API_KEY == "patched-key"
    assert cfg.trading_base_url == PAPER_ALPACA_BASE_URL
    assert "patched-key" not in repr(cfg)


def test_dotenv_file_loaded_without_overriding_explicit_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ALPACA_API_KEY=from-dotenv\n"
        "ALPACA_SECRET_KEY=secret-from-dotenv\n"
        "PRIORITY_SYMBOLS=nvda, msft\n"
        "UNKNOWN_OLD_BOT_VAR=ignored\n"
    )
    s = load_settings(
        {"ALPACA_API_KEY": "from-process-env"},
        env_file=env_file,
        require_runtime_secrets=True,
    )
    assert s.ALPACA_API_KEY == "from-process-env"
    assert s.ALPACA_SECRET_KEY == "secret-from-dotenv"
    assert s.PRIORITY_SYMBOLS == ["NVDA", "MSFT"]


def test_env_example_matches_settings_and_contains_no_secrets() -> None:
    from dotenv import dotenv_values

    from stock_agent.config import _ENV_ALIASES, _SECRET_EXTRA_NAMES, _SECRET_SUFFIXES

    repo_root = Path(__file__).resolve().parent.parent
    env_example = repo_root / ".env.example"
    assert env_example.is_file()

    parsed = dotenv_values(env_example)
    assert parsed, ".env.example should define key settings"

    supported_env_names = set(Settings.model_fields) | set(_ENV_ALIASES.values())
    unknown_keys = set(parsed) - supported_env_names
    assert not unknown_keys, f".env.example contains unknown settings: {sorted(unknown_keys)}"

    for key, val in parsed.items():
        if key.endswith(_SECRET_SUFFIXES) or key in _SECRET_EXTRA_NAMES:
            assert (val or "") == "", f".env.example must keep {key} empty"

    clean_env = {k: (v or "") for k, v in parsed.items()}
    s = load_settings(clean_env)
    assert s.MODE == "PAPER"
    assert s.OPTIONS_MODE == "defined_risk"
    assert s.trading_base_url == PAPER_ALPACA_BASE_URL

    with pytest.raises(SettingsError, match="ALPACA_API_KEY"):
        load_settings(clean_env, require_runtime_secrets=True)


def test_gitignore_and_gitleaks_hygiene() -> None:
    import re
    import tomllib

    repo_root = Path(__file__).resolve().parent.parent
    assert not (repo_root / ".mcp.json").exists()

    gitignore_lines = {
        line.strip()
        for line in (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert ".env" in gitignore_lines
    assert ".mcp.json" in gitignore_lines
    assert "dashboard/data/" not in gitignore_lines

    gitleaks_cfg = tomllib.loads((repo_root / ".gitleaks.toml").read_text(encoding="utf-8"))
    assert gitleaks_cfg.get("extend", {}).get("useDefault") is True
    allowlist = gitleaks_cfg.get("allowlist", {})
    assert "paths" not in allowlist
    commits = allowlist.get("commits", [])
    assert commits
    assert all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in commits)


def test_requirements_files_are_exact_pinned_and_lightweight() -> None:
    import re

    repo_root = Path(__file__).resolve().parent.parent
    pin_re = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)$")
    forbidden_runtime = {
        "aiohttp",
        "fastapi",
        "numpy",
        "pandas",
        "playwright",
        "requests",
        "uvicorn",
        "yfinance",
    }

    for filename in ("requirements.txt", "requirements-dev.txt"):
        lines = [
            line.strip()
            for line in (repo_root / filename).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert lines, f"{filename} should not be empty"
        pkgs: set[str] = set()
        for line in lines:
            assert "--hash" not in line, f"{filename} must work with plain pip install -r"
            match = pin_re.match(line)
            assert match is not None, f"Unpinned line in {filename}: {line!r}"
            pkgs.add(match.group(1).lower().replace("_", "-"))

        if filename == "requirements.txt":
            assert not (pkgs & forbidden_runtime)
            assert {"httpx", "pydantic", "python-dotenv"} <= pkgs
        else:
            assert {"pytest", "ruff"} <= pkgs


def test_owner_checklist_and_readme_cover_security_controls() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    checklist = (repo_root / "docs" / "revamp" / "OWNER_CHECKLIST.md").read_text(encoding="utf-8")
    readme = (repo_root / "README.md").read_text(encoding="utf-8")

    for required_term in (
        "@BotFather",
        ".mcp.json",
        "TIPRANKS_API_KEY",
        "FMP_API_KEY",
        "FINNHUB_API_KEY",
        "PRIVATE_KEY",
        "nginx",
        "OPTIONS_MODE",
        "STOCK_TRADING_PAUSED",
        "STOCK_AGENT_LIVE_CONFIRMED",
        "MARGIN_ENABLED",
        "MAX_GROSS_EXPOSURE_PCT",
        "MARGIN_MIN_EXCESS_PCT",
        "MARGIN_TRIM_EXCESS_PCT",
        "MARGIN_INTEREST_RATE_PCT",
        "MAX_TOTAL_EXPOSURE_PCT",
        "clean_server_env.py",
    ):
        assert required_term in checklist, f"OWNER_CHECKLIST.md missing {required_term}"

    for required_readme in (
        "OWNER_CHECKLIST.md",
        "STOCK_AGENT_LIVE_CONFIRMED",
        "STOCK_TRADING_PAUSED",
        "OPTIONS_MODE",
        "close_only",
        "owner_focus.json",
        "MARGIN_ENABLED",
        "MAX_GROSS_EXPOSURE_PCT",
        "MARGIN_MIN_EXCESS_PCT",
        "MARGIN_TRIM_EXCESS_PCT",
        "MARGIN_INTEREST_RATE_PCT",
        "MAX_TOTAL_EXPOSURE_PCT",
        "clean_server_env.py",
    ):
        assert required_readme in readme, f"README.md missing {required_readme}"

    env_example_text = (repo_root / ".env.example").read_text(encoding="utf-8")
    assert "close_only" in env_example_text
    assert "`legacy`" not in env_example_text
    assert "`legacy`" not in readme
    for margin_var in (
        "MARGIN_ENABLED=true",
        "MAX_GROSS_EXPOSURE_PCT=2.0",
        "MARGIN_MIN_EXCESS_PCT=0.25",
        "MARGIN_TRIM_EXCESS_PCT=0.15",
        "MARGIN_INTEREST_RATE_PCT=6.5",
    ):
        assert margin_var in env_example_text

    for deleted_module in (
        "data_feed.py",
        "tipranks_client.py",
        "news_feed.py",
        "sentiment.py",
        "analyst_trends.py",
        "options_data.py",
        "options_engine.py",
        "options_executor.py",
        "options_models.py",
        "options_portfolio.py",
        "portfolio.py",
    ):
        assert deleted_module not in readme, f"README.md still references {deleted_module}"


