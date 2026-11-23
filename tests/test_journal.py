import gzip
import json
from datetime import datetime, timezone

from stock_agent.journal import Journal


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_journal_append_assigns_increasing_seq_and_reads_back(tmp_path):
    j = Journal(tmp_path)
    assert j.append("decision", symbol="AAA", action="buy") == 1
    assert j.append("fill", symbol="AAA", qty=10) == 2
    recs = list(j.read())
    assert [r["seq"] for r in recs] == [1, 2]
    assert recs[1]["kind"] == "fill" and recs[1]["qty"] == 10 and recs[1]["v"] == 1


def test_journal_seq_continues_after_restart(tmp_path):
    Journal(tmp_path).append("decision", a=1)
    assert Journal(tmp_path).append("decision", a=2) == 2


def test_journal_rotation_gzips_old_months_and_keeps_everything(tmp_path):
    clk = _Clock(datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc))
    j = Journal(tmp_path, clock=clk)
    j.append("decision", n=1)
    clk.t = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)
    j.append("decision", n=2)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["journal-2026-10.jsonl.gz", "journal-2026-11.jsonl"]
    with gzip.open(tmp_path / "journal-2026-10.jsonl.gz", "rt") as fh:
        assert json.loads(fh.readline())["n"] == 1
    assert [r["n"] for r in j.read()] == [1, 2]


def test_journal_failure_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    j = Journal(blocker / "sub")          # cannot create a directory under a file
    assert j.append("order", x=1) is None
    h = j.health()
    assert not h.ok and h.errors == 1


def test_journal_serialises_rich_types_and_skips_torn_line(tmp_path):
    from decimal import Decimal
    j = Journal(tmp_path)
    j.append("fill", price=Decimal("1.25"), at=datetime(2026, 1, 2, tzinfo=timezone.utc), tags={"a"})
    with open(j.files()[0], "a") as fh:
        fh.write('{"seq": 2, "kind": "torn"')
    recs = list(j.read())
    assert len(recs) == 1 and recs[0]["price"] == "1.25" and recs[0]["tags"] == ["a"]


def test_journal_filters_by_kind_and_reports_size(tmp_path):
    j = Journal(tmp_path, max_mb=0.0001)
    j.append("shadow_stop", symbol="AAA", rule="12pct")
    j.append("news_reaction", symbol="AAA", story_id="s1")
    assert [r["kind"] for r in j.read(kinds={"news_reaction"})] == ["news_reaction"]
    assert j.health().near_cap
