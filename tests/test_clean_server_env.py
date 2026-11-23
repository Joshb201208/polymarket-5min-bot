"""Unit tests for scripts/clean_server_env.py using synthetic `.env` fixtures."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import stat
import sys

import pytest

from scripts import clean_server_env
from scripts.clean_server_env import (
    OLD_BOT_RETIRED_NAMES,
    REQUIRED_KEPT_NAMES,
    RETIRED_STOCK_AGENT_NAMES,
    UNCONDITIONAL_REMOVE_NAMES,
    main,
)


def _synthetic_server_env_text(*, extra_lines: str = "") -> str:
    """Build a synthetic 41-assignment `.env` matching the server's variable structure."""
    lines = [
        "# Server environment configuration",
        "TELEGRAM_BOT_TOKEN=",
        'TELEGRAM_CHAT_ID=""',
        "TRADING_MODE=live",
        "POLYMARKET_API_KEY=",
        "POLYMARKET_API_SECRET=",
        "POLYMARKET_API_PASSPHRASE=",
        "PRIVATE_KEY=SYNTH_SECRET_WALLET_VALUE_DO_NOT_PRINT_01",
        "FUNDER_ADDRESS=SYNTH_SECRET_FUNDER_VALUE_DO_NOT_PRINT_02",
        "STARTING_BANKROLL=SYNTH_VAL_03",
        "MAX_BET_PCT=0.05",
        "MAX_GAME_EXPOSURE_PCT=0.10",
        "MAX_TOTAL_EXPOSURE_PCT=1.00",
        "SCAN_INTERVAL=60",
        "EXIT_CHECK_INTERVAL=30",
        "PRE_TIPOFF_MINUTES=15",
        "MIN_GAME_EDGE=0.04",
        "MIN_FUTURES_EDGE=0.06",
        "ODDS_API_KEY=SYNTH_SECRET_ODDS_DO_NOT_PRINT_04",
        "BALLDONTLIE_API_KEY=SYNTH_SECRET_BDL_DO_NOT_PRINT_05",
        "PROXY_HOST=SYNTH_PROXY_HOST_06",
        "PROXY_PORT=12345",
        "PROXY_USER=SYNTH_PROXY_USER_07",
        "PROXY_PASS=SYNTH_SECRET_PROXY_PASS_08#with_hash",
        "NBA_SEASON=2025-26",
        "LOG_LEVEL=INFO",
        "NHL_TRADING_MODE=paper",
        "TWITTER_BEARER_TOKEN=SYNTH_SECRET_TWITTER_DO_NOT_PRINT_09",
        "",
        "# Keep comment above active stock_agent settings",
        "ALPACA_API_KEY=SYNTH_SECRET_ALPACA_KEY_DO_NOT_PRINT_10",
        "ALPACA_SECRET_KEY=SYNTH_SECRET_ALPACA_SEC_DO_NOT_PRINT_11",
        "PERPLEXITY_API_KEY=SYNTH_SECRET_PPLX_DO_NOT_PRINT_12",
        "OPENAI_API_KEY=SYNTH_SECRET_OPENAI_DO_NOT_PRINT_13",
        "FMP_API_KEY=SYNTH_SECRET_FMP_DO_NOT_PRINT_14",
        "FINNHUB_API_KEY=SYNTH_SECRET_FINNHUB_DO_NOT_PRINT_15",
        "STOCK_AGENT_MODE=PAPER",
        "DISCORD_BOT_TOKEN=SYNTH_SECRET_DISCORD_DO_NOT_PRINT_16",
        "TIPRANKS_EMAIL=SYNTH_SECRET_TR_EMAIL_DO_NOT_PRINT_17",
        "TIPRANKS_PASSWORD=SYNTH_SECRET_TR_PASS_DO_NOT_PRINT_18",
        "TIPRANKS_EMAIL=SYNTH_SECRET_TR_EMAIL_DO_NOT_PRINT_17",
        "TIPRANKS_PASSWORD=SYNTH_SECRET_TR_PASS_DO_NOT_PRINT_18",
        "ANTHROPIC_API_KEY=SYNTH_SECRET_ANTHROPIC_DO_NOT_PRINT_19",
        "EVENTS_STARTING_BANKROLL=SYNTH_VAL_20",
    ]
    if extra_lines:
        lines.append(extra_lines)
    return "\n".join(lines) + "\n"


def _assert_no_secret_leaked(output: str) -> None:
    assert "SYNTH_" not in output
    assert "DO_NOT_PRINT" not in output


def test_script_uses_python_stdlib_only() -> None:
    script_path = Path(clean_server_env.__file__).resolve()
    tree = ast.parse(script_path.read_text(encoding="utf-8"))
    stdlib = sys.stdlib_module_names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in stdlib
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            assert node.module.split(".")[0] in stdlib


def test_allowlists_cover_exact_retired_and_required_names() -> None:
    assert len(OLD_BOT_RETIRED_NAMES) == 27
    assert RETIRED_STOCK_AGENT_NAMES == ("MAX_TOTAL_EXPOSURE_PCT",)
    assert len(UNCONDITIONAL_REMOVE_NAMES) == 28
    assert len(REQUIRED_KEPT_NAMES) == 9
    assert not (UNCONDITIONAL_REMOVE_NAMES & set(REQUIRED_KEPT_NAMES))


def test_dry_run_is_default_never_touches_disk_and_never_prints_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    original = _synthetic_server_env_text()
    env_file.write_text(original, encoding="utf-8")
    os.chmod(env_file, 0o640)

    rc = main([str(env_file)])
    captured = capsys.readouterr()

    assert rc == 0
    _assert_no_secret_leaked(captured.out + captured.err)
    assert "[DRY-RUN]" in captured.out
    assert "MAX_TOTAL_EXPOSURE_PCT" in captured.out
    assert "PRIVATE_KEY" in captured.out
    assert "TIPRANKS_EMAIL (x2)" in captured.out
    assert "TELEGRAM_BOT_TOKEN" in captured.out
    assert env_file.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".env.bak.*")) == []


def test_apply_writes_mode_600_backup_preserves_order_comments_mode_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    original = _synthetic_server_env_text(
        extra_lines="OPTIONS_MODE=defined_risk\nCUSTOM_UNLISTED_SETTING=SYNTH_KEEP_ME"
    )
    env_file.write_text(original, encoding="utf-8")
    os.chmod(env_file, 0o640)
    before_stat = os.stat(env_file)

    rc = main([str(env_file), "--apply"])
    captured = capsys.readouterr()

    assert rc == 0
    _assert_no_secret_leaked(captured.out + captured.err)

    backups = list(tmp_path.glob(".env.bak.*"))
    assert len(backups) == 1
    backup = backups[0]
    assert backup.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600

    after_stat = os.stat(env_file)
    assert stat.S_IMODE(after_stat.st_mode) == 0o640
    assert after_stat.st_uid == before_stat.st_uid
    assert after_stat.st_gid == before_stat.st_gid

    cleaned_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert cleaned_lines == [
        "# Server environment configuration",
        "",
        "# Keep comment above active stock_agent settings",
        "ALPACA_API_KEY=SYNTH_SECRET_ALPACA_KEY_DO_NOT_PRINT_10",
        "ALPACA_SECRET_KEY=SYNTH_SECRET_ALPACA_SEC_DO_NOT_PRINT_11",
        "PERPLEXITY_API_KEY=SYNTH_SECRET_PPLX_DO_NOT_PRINT_12",
        "OPENAI_API_KEY=SYNTH_SECRET_OPENAI_DO_NOT_PRINT_13",
        "FMP_API_KEY=SYNTH_SECRET_FMP_DO_NOT_PRINT_14",
        "FINNHUB_API_KEY=SYNTH_SECRET_FINNHUB_DO_NOT_PRINT_15",
        "STOCK_AGENT_MODE=PAPER",
        "DISCORD_BOT_TOKEN=SYNTH_SECRET_DISCORD_DO_NOT_PRINT_16",
        "ANTHROPIC_API_KEY=SYNTH_SECRET_ANTHROPIC_DO_NOT_PRINT_19",
        "OPTIONS_MODE=defined_risk",
        "CUSTOM_UNLISTED_SETTING=SYNTH_KEEP_ME",
    ]

    # Second `--apply` is a pure idempotent no-op and writes no second backup.
    cleaned_bytes = env_file.read_bytes()
    rc_second = main([str(env_file), "--apply"])
    captured_second = capsys.readouterr()
    assert rc_second == 0
    _assert_no_secret_leaked(captured_second.out + captured_second.err)
    assert env_file.read_bytes() == cleaned_bytes
    assert len(list(tmp_path.glob(".env.bak.*"))) == 1


def test_non_empty_telegram_settings_are_kept_while_empty_ones_removed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    text = _synthetic_server_env_text().replace(
        "TELEGRAM_BOT_TOKEN=",
        'TELEGRAM_BOT_TOKEN="SYNTH_ACTIVE_TELEGRAM_TOKEN"',
    )
    env_file.write_text(text, encoding="utf-8")

    rc = main([str(env_file), "--apply"])
    captured = capsys.readouterr()
    assert rc == 0
    _assert_no_secret_leaked(captured.out + captured.err)

    result = env_file.read_text(encoding="utf-8")
    assert 'TELEGRAM_BOT_TOKEN="SYNTH_ACTIVE_TELEGRAM_TOKEN"' in result
    assert "TELEGRAM_CHAT_ID" not in result


def test_exact_duplicate_kept_assignment_collapses_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    text = _synthetic_server_env_text(
        extra_lines="STOCK_AGENT_MODE=PAPER\nALPACA_API_KEY=SYNTH_SECRET_ALPACA_KEY_DO_NOT_PRINT_10"
    )
    env_file.write_text(text, encoding="utf-8")

    rc = main([str(env_file), "--apply"])
    captured = capsys.readouterr()
    assert rc == 0
    _assert_no_secret_leaked(captured.out + captured.err)
    assert "STOCK_AGENT_MODE" in captured.out

    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines.count("STOCK_AGENT_MODE=PAPER") == 1
    assert lines.count("ALPACA_API_KEY=SYNTH_SECRET_ALPACA_KEY_DO_NOT_PRINT_10") == 1


@pytest.mark.parametrize(
    "extra",
    [
        "STOCK_AGENT_MODE=LIVE",
        "ALPACA_API_KEY=SYNTH_DIFFERENT_CONFLICTING_SECRET",
        "TELEGRAM_BOT_TOKEN=SYNTH_LATE_TELEGRAM_SECRET",
    ],
)
def test_conflicting_duplicate_kept_assignment_fails_closed_without_leaking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: str
) -> None:
    env_file = tmp_path / ".env"
    original = _synthetic_server_env_text(extra_lines=extra)
    env_file.write_text(original, encoding="utf-8")

    rc = main([str(env_file), "--apply"])
    captured = capsys.readouterr()

    assert rc != 0
    _assert_no_secret_leaked(captured.out + captured.err)
    assert "Conflicting duplicate assignments" in captured.err
    assert env_file.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".env.bak.*")) == []


@pytest.mark.parametrize("required_name", REQUIRED_KEPT_NAMES)
def test_missing_or_empty_required_variable_refuses_to_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], required_name: str
) -> None:
    base = _synthetic_server_env_text()

    # Case 1: required variable missing altogether
    missing_file = tmp_path / "missing.env"
    missing_text = "\n".join(
        line for line in base.splitlines() if not line.startswith(f"{required_name}=")
    ) + "\n"
    missing_file.write_text(missing_text, encoding="utf-8")
    rc_missing = main([str(missing_file), "--apply"])
    out_missing = capsys.readouterr()
    assert rc_missing != 0
    assert required_name in out_missing.err
    _assert_no_secret_leaked(out_missing.out + out_missing.err)
    assert missing_file.read_text(encoding="utf-8") == missing_text

    # Case 2: required variable present but blank
    empty_file = tmp_path / "empty.env"
    empty_text = "\n".join(
        f"{required_name}=" if line.startswith(f"{required_name}=") else line
        for line in base.splitlines()
    ) + "\n"
    empty_file.write_text(empty_text, encoding="utf-8")
    rc_empty = main([str(empty_file), "--apply"])
    out_empty = capsys.readouterr()
    assert rc_empty != 0
    assert required_name in out_empty.err
    _assert_no_secret_leaked(out_empty.out + out_empty.err)
    assert empty_file.read_text(encoding="utf-8") == empty_text


@pytest.mark.parametrize(
    "bad_line",
    [
        "NOT_AN_ASSIGNMENT SYNTH_RAW_SECRET_LINE",
        'OPENAI_API_KEY="SYNTH_UNCLOSED_QUOTE',
        "CUSTOM_VAR=SYNTH_CONTINUATION_SECRET\\",
    ],
)
def test_syntax_doubt_exits_nonzero_without_touching_file_or_leaking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad_line: str
) -> None:
    env_file = tmp_path / ".env"
    original = _synthetic_server_env_text(extra_lines=bad_line)
    env_file.write_text(original, encoding="utf-8")

    rc = main([str(env_file), "--apply"])
    captured = capsys.readouterr()

    assert rc != 0
    _assert_no_secret_leaked(captured.out + captured.err)
    assert env_file.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".env.bak.*")) == []


def test_refuses_symlink_directory_or_missing_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_env = tmp_path / "real.env"
    real_env.write_text(_synthetic_server_env_text(), encoding="utf-8")
    link_env = tmp_path / "link.env"
    link_env.symlink_to(real_env)

    assert main([str(link_env), "--apply"]) != 0
    assert main([str(tmp_path), "--apply"]) != 0
    assert main([str(tmp_path / "nope.env"), "--apply"]) != 0
    captured = capsys.readouterr()
    _assert_no_secret_leaked(captured.out + captured.err)


def test_cli_subprocess_dry_run_and_apply(tmp_path: Path) -> None:
    import subprocess

    script_path = Path(clean_server_env.__file__).resolve()
    env_file = tmp_path / ".env"
    original = _synthetic_server_env_text()
    env_file.write_text(original, encoding="utf-8")
    os.chmod(env_file, 0o600)

    dry = subprocess.run(
        [sys.executable, str(script_path), str(env_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.returncode == 0
    _assert_no_secret_leaked(dry.stdout + dry.stderr)
    assert env_file.read_text(encoding="utf-8") == original

    applied = subprocess.run(
        [sys.executable, str(script_path), str(env_file), "--apply"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0
    _assert_no_secret_leaked(applied.stdout + applied.stderr)
    assert "MAX_TOTAL_EXPOSURE_PCT" not in env_file.read_text(encoding="utf-8")
    assert "ALPACA_API_KEY=SYNTH_SECRET_ALPACA_KEY_DO_NOT_PRINT_10" in env_file.read_text(
        encoding="utf-8"
    )

