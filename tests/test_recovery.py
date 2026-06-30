import os
import sqlite3

from demo_cli import recovery


def _make_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(1, "a"), (2, "b"), (3, "c")])
    con.commit()
    con.close()


def test_no_default_target(tmp_path, monkeypatch):
    # A destructive command with nothing to resolve must yield no target.
    monkeypatch.chdir(tmp_path)
    assert recovery.resolve_target("rm -rf ./build") is None


def test_resolve_named_db(tmp_path):
    db = tmp_path / "app.db"
    _make_db(str(db))
    t = recovery.resolve_target(f"sqlite3 {db} 'DELETE FROM t'")
    assert t is not None and t.kind == "sqlite" and t.ref == str(db)


def test_resolve_postgres_url():
    t = recovery.resolve_target("psql postgres://u:p@h/db -c 'DELETE FROM t'")
    assert t.kind == "postgres" and t.ref.startswith("postgres://")


def test_snapshot_and_restore_sqlite(tmp_path):
    db = tmp_path / "app.db"
    _make_db(str(db))
    rec_dir = str(tmp_path / "rec")
    target = recovery.Target("sqlite", str(db), str(db))
    entry = recovery.snapshot(target, rec_dir)
    assert entry and os.path.exists(entry["recovery_point"])

    # mutate then restore
    con = sqlite3.connect(str(db))
    con.execute("DELETE FROM t")
    con.commit()
    con.close()
    assert recovery.snapshot is not None
    assert recovery.restore_entry(entry) is True
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
    con.close()


def test_snapshot_strategy_none_captures_nothing(tmp_path):
    db = tmp_path / "app.db"
    _make_db(str(db))
    target = recovery.Target("sqlite", str(db), str(db))
    assert recovery.snapshot(target, str(tmp_path / "rec"), strategy="none") is None
