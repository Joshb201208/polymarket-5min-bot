import shutil
from pathlib import Path

from stock_agent.state_boot import build_ledger, open_ledger, prepare_ledger
from tests.ledger_helpers import StubBroker, run

FIX = Path(__file__).parent / "fixtures" / "state_v0"


def test_open_ledger_migrates_legacy_on_first_start(tmp_path):
    for f in FIX.iterdir():
        shutil.copy(f, tmp_path / f.name)
    boot = open_ledger(tmp_path, broker=StubBroker())
    assert boot.health.created and boot.health.migrated_v0 and boot.needs_rebuild
    assert len(boot.ledger.all_open_meta()) == 3
    boot.ledger.store.close()
    again = open_ledger(tmp_path, broker=StubBroker())
    assert not again.health.created and again.migration is None and not again.needs_rebuild


def test_prepare_runs_peer_migrations(tmp_path):
    led = build_ledger(tmp_path, broker=StubBroker())
    led.store.register_migration(1, "peer", lambda c: c.execute("CREATE TABLE peer_t (x INTEGER)"))
    boot = prepare_ledger(led, tmp_path)
    assert boot.migration is None
    led.store.query("SELECT * FROM peer_t")
    run(led.refresh("startup", rebuild=boot.needs_rebuild))


def test_quarantined_store_needs_rebuild(tmp_path):
    (tmp_path / "state.db").write_bytes(b"garbage" * 300)
    boot = open_ledger(tmp_path, broker=StubBroker())
    assert boot.health.quarantined and boot.needs_rebuild and boot.ledger.trading_block()
