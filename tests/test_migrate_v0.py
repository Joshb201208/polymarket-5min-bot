import json
import shutil
from pathlib import Path

from stock_agent.ledger import ThesisRole
from stock_agent.migrate_v0 import migrate_v0
from tests.ledger_helpers import StubBroker, make_ledger, run

FIX = Path(__file__).parent / "fixtures" / "state_v0"


def _setup(tmp_path):
    for f in FIX.iterdir():
        shutil.copy(f, tmp_path / f.name)


def test_migrate_imports_metadata_theses_and_archives(tmp_path):
    _setup(tmp_path)
    led = make_ledger(tmp_path)
    rep = migrate_v0(tmp_path, led.store)
    assert rep.ran and rep.positions == 3 and rep.entry_theses == 2 and rep.legacy_unverified == 2
    assert rep.held_review == 1 and rep.entry_candidates == 1
    assert rep.equity_marks == 2 and rep.trades_archived == 2 and rep.options_open == 1
    assert rep.jobs == ["weekly:2026-W46"]
    a = led.open_position_meta("AAA")
    assert a.sector == "Technology" and float(a.stop_price) == 90.0 and a.opened_source == "v0"
    assert led.get_thesis(a.entry_thesis_id).role == ThesisRole.ENTRY_CANDIDATE
    h = led.open_position_meta("HHH")
    assert h.entry_thesis_id is None and h.opened_at is None          # placeholder never trusted
    assert led.current_thesis("AAA").role == ThesisRole.HELD_REVIEW
    assert led.current_thesis("DDD").role == ThesisRole.ENTRY_CANDIDATE
    assert led.current_thesis("HHH") is None
    assert all(m.quality == "legacy" for m in led.equity_marks())
    names = {r["name"] for r in led.store.query("SELECT name FROM legacy_archive")}
    assert {"v0.trade_history", "v0.daily_summaries", "v0.options_open", "v0.options_closed"} <= names
    # originals untouched and copied
    assert json.loads((tmp_path / "portfolio.json").read_text())["positions"]
    assert (tmp_path / "legacy_v0" / "portfolio.json").exists()


def test_migrate_is_idempotent(tmp_path):
    _setup(tmp_path)
    led = make_ledger(tmp_path)
    migrate_v0(tmp_path, led.store)
    again = migrate_v0(tmp_path, led.store)
    assert not again.ran and again.skipped_reason == "already migrated"
    assert len(led.all_open_meta()) == 3


def test_migrate_dry_run_writes_nothing(tmp_path):
    _setup(tmp_path)
    led = make_ledger(tmp_path)
    rep = migrate_v0(tmp_path, led.store, dry_run=True)
    assert rep.ran and rep.dry_run and rep.positions == 3
    assert led.all_open_meta() == [] and led.store.get_meta("migrated_v0_at") is None
    assert not (tmp_path / "legacy_v0").exists()


def test_migrate_unreadable_legacy_is_skipped(tmp_path):
    (tmp_path / "portfolio.json").write_text("{nope")
    led = make_ledger(tmp_path)
    rep = migrate_v0(tmp_path, led.store)
    assert not rep.ran and rep.warnings


def test_reconcile_after_migration_uses_broker_fill_dates(tmp_path):
    _setup(tmp_path)
    b = StubBroker()
    b.fill("AAA", "buy", 10, 100, ts="2026-04-01T14:30:00Z")
    b.fill("HHH", "buy", 5, 200, ts="2026-04-23T17:56:00Z")      # bought long before the placeholder date
    b.pos("AAA", 10, 100)
    b.pos("HHH", 5, 200)
    led = make_ledger(tmp_path, broker=b)
    migrate_v0(tmp_path, led.store)
    snap = run(led.refresh("first", rebuild=True))
    assert led.open_position_meta("HHH").opened_at.date().isoformat() == "2026-04-23"
    assert led.open_position_meta("AAA").opened_source == "v0_verified"
    assert led.open_position_meta("BBB") is None                 # ledger-only position closed
    assert any(d.kind.value == "entry_date_corrected" and d.symbol == "HHH" for d in snap.diffs)
    assert snap.holding("AAA").meta.sector == "Technology"
