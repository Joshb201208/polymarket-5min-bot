from stock_agent.config import Config
from stock_agent.portfolio import Portfolio
from tests.ledger_helpers import StubBroker, make_ledger, run


class Cfg(Config):
    pass


def _portfolio(tmp_path):
    cfg = Cfg()
    cfg.DATA_DIR = tmp_path
    return Portfolio(cfg)


def test_snapshot_listener_mirrors_ledger_into_legacy_view(tmp_path):
    b = StubBroker(cash=5000)
    b.pos("AAA", 10, 100, 110)
    b.pos("MMM", 0.5, 200)
    b.pos("PPP", -3, 10)
    b.pos("GSX261127C00920000", 1, 2, 3)
    led = make_ledger(tmp_path, broker=b)
    pf = _portfolio(tmp_path)
    led.add_snapshot_listener(pf.sync_from_snapshot)
    snap = run(led.refresh("x", rebuild=True))
    led.set_sector(snap.holding("AAA").meta.position_id, "Technology")
    run(led.refresh("y"))
    assert [p.symbol for p in pf.state.positions] == ["AAA"]   # long whole-share stock only
    p = pf.state.positions[0]
    assert p.shares == 10 and p.current_price == 110 and p.sector == "Technology" and pf.state.cash == 5000


def test_failed_refresh_leaves_legacy_view(tmp_path):
    b = StubBroker()
    b.pos("AAA", 10, 100)
    led = make_ledger(tmp_path, broker=b)
    pf = _portfolio(tmp_path)
    led.add_snapshot_listener(pf.sync_from_snapshot)
    run(led.refresh("x", rebuild=True))
    b.fail = {"get_positions"}
    try:
        run(led.refresh("fail"))
    except Exception:
        pass
    assert pf.get_position("AAA") is not None


def test_unreadable_portfolio_json_is_moved_aside_not_overwritten(tmp_path):
    (tmp_path / "portfolio.json").write_text("{not json")
    pf = _portfolio(tmp_path)
    pf._save()
    aside = list(tmp_path.glob("portfolio.json.unreadable-*"))
    assert len(aside) == 1 and aside[0].read_text() == "{not json"


def test_in_memory_view_never_touches_legacy_files(tmp_path):
    (tmp_path / "portfolio.json").write_text("{not json")
    cfg = Cfg()
    cfg.DATA_DIR = tmp_path
    pf = Portfolio(cfg, persist=False)
    b = StubBroker()
    b.pos("AAA", 10, 100)
    led = make_ledger(tmp_path, broker=b)
    led.add_snapshot_listener(pf.sync_from_snapshot)
    run(led.refresh("x", rebuild=True))
    assert pf.get_position("AAA") is not None
    assert (tmp_path / "portfolio.json").read_text() == "{not json"
    assert sorted(p.name for p in tmp_path.glob("portfolio*")) == ["portfolio.json"]
