import json
import sqlite3

import pytest

from tests.fakes.state import corrupt


@pytest.mark.parametrize("how", ["garbage", "truncate", "empty"])
def test_corrupt_json_is_unreadable(tmp_path, how):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"positions": [{"symbol": "AAA", "qty": 1}] * 20}))
    corrupt(p, how)
    with pytest.raises(ValueError):
        json.loads(p.read_text(errors="replace"))


def test_corrupt_sqlite_tail_is_detected(tmp_path):
    p = tmp_path / "s.db"
    con = sqlite3.connect(p)
    con.execute("create table t(x)")
    con.executemany("insert into t values (?)", [("y" * 200,)] * 200)
    con.commit()
    con.close()
    corrupt(p, "garbage")
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(p).execute("pragma integrity_check").fetchall()


def test_delete_and_world_data_dir(world, tmp_path):
    assert world.data_dir == tmp_path
    p = tmp_path / "x"
    p.write_text("1")
    corrupt(p, "delete")
    assert not p.exists()
