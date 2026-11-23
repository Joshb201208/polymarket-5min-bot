"""Offline safety checks for the database portion of the VPS snapshot."""

import json
from contextlib import closing
import sqlite3

from scripts.snapshot_ledger import summarize
from stock_agent.state_store import StateStore


def test_missing_ledger_snapshot_does_not_create_database(tmp_path):
    path = tmp_path / "state.db"
    assert summarize(path) == {"status": "state database not found"}
    assert not path.exists()


def test_ledger_snapshot_reads_committed_wal_without_exporting_payloads(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    store.open()
    try:
        with closing(sqlite3.connect(path)) as writer:
            assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            writer.execute(
                "INSERT INTO equity_marks(session_date, quality, equity, taken_at, extra_json) "
                "VALUES (?, ?, ?, ?, ?)",
                ("2026-11-23", "close", 1000, "2026-11-23T21:01:00Z", '{"private_payload": "not exported"}'),
            )
            writer.commit()
            summary = summarize(path)
            assert summary["status"] == "ok"
            assert summary["recent_close_marks"][0]["equity"] == 1000
            assert "not exported" not in json.dumps(summary)
            assert not writer.execute("SELECT count(*) FROM order_intents").fetchone()[0]
    finally:
        store.close()
