import sqlite3

import pytest

from stock_agent.journal import Journal
from stock_agent.state_store import SCHEMA_VERSION, StateStore


def _store(tmp_path):
    return StateStore(tmp_path / "state.db", Journal(tmp_path / "j"))


def test_store_open_creates_schema_v1(tmp_path):
    s = _store(tmp_path)
    h = s.open()
    assert h.ok and h.created and h.trading_block is None
    assert s.get_meta("schema:core") == str(SCHEMA_VERSION)
    tables = {r[0] for r in s.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"positions_meta", "theses", "order_intents", "job_runs", "equity_marks", "cash_flows"} <= tables


def test_store_reopen_keeps_data(tmp_path):
    s = _store(tmp_path)
    s.open()
    s.kv_set("x", {"a": 1})
    s.close()
    s2 = _store(tmp_path)
    h = s2.open()
    assert not h.created and s2.kv_get("x") == {"a": 1}


def test_store_corrupt_file_quarantined_and_trading_blocked(tmp_path):
    (tmp_path / "state.db").write_bytes(b"this is not a database" * 100)
    s = _store(tmp_path)
    h = s.open()
    assert h.quarantined is not None and h.quarantined.exists()
    assert h.created and not h.ok
    assert "rebuild from broker" in s.trading_block()


def test_store_never_fresh_start_silently_block_survives_restart(tmp_path):
    (tmp_path / "state.db").write_bytes(b"garbage" * 200)
    s = _store(tmp_path)
    s.open()
    s.close()
    s2 = _store(tmp_path)
    h = s2.open()
    assert h.trading_block and "rebuild" in h.trading_block
    s2.clear_trading_block(by="test")
    s2.close()
    s3 = _store(tmp_path)
    assert s3.open().trading_block is None


def test_store_newer_schema_is_quarantined(tmp_path):
    s = _store(tmp_path)
    s.open()
    s.set_meta("schema:core", str(SCHEMA_VERSION + 5))
    s.close()
    s2 = _store(tmp_path)
    h = s2.open()
    assert h.quarantined is not None and s2.trading_block()


def test_transaction_rollback_on_error(tmp_path):
    s = _store(tmp_path)
    s.open()
    with pytest.raises(RuntimeError):
        with s.transaction() as c:
            c.execute("INSERT INTO kv(key, value_json, updated_at) VALUES ('k','1','t')")
            raise RuntimeError("boom")
    assert s.kv_get("k") is None


def test_register_migration_for_other_area_runs_once(tmp_path):
    calls = []

    def mig(c):
        calls.append(1)
        c.execute("CREATE TABLE option_structures (id TEXT PRIMARY KEY)")

    s = _store(tmp_path)
    s.register_migration(1, "options", mig)
    s.open()
    s.close()
    s2 = _store(tmp_path)
    s2.register_migration(1, "options", mig)
    s2.open()
    assert calls == [1]
    assert s2.get_meta("schema:options") == "1"


def test_store_backup_rotation_keeps_n(tmp_path):
    from datetime import datetime, timezone
    s = StateStore(tmp_path / "state.db", None, backup_keep=2)
    s.open()
    for d in (1, 2, 3):
        p = s.backup(datetime(2026, 11, d, tzinfo=timezone.utc))
        c = sqlite3.connect(p)
        assert c.execute("SELECT count(*) FROM meta").fetchone()[0] >= 1
        c.close()
    names = sorted(x.name for x in (tmp_path / "backups").iterdir())
    assert names == ["state-20261102.db", "state-20261103.db"]
