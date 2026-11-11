import sqlite3
import time

import pytest

from bridge import session_store as store


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv(store.STORE_DIR_ENV, str(tmp_path))
    conn = store._db(import_files=False)
    conn.close()


def stored():
    conn = store._db(import_files=False)
    try:
        return [tuple(row) for row in conn.execute("SELECT key, turn, text FROM fleet_turns ORDER BY id")]
    finally:
        conn.close()


def test_drains_once_even_after_replay(isolated_store, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(store, "_db", lambda **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
        for n in range(3):
            assert store.record_fleet_turn("worker", {"turn": n, "text": str(n)}) is False
    with open(store._spool_path(), encoding="utf-8") as f:
        original = f.read()
    assert store.drain_fleet_spool(store) == 3
    assert store.drain_fleet_spool(store) == 0
    with open(store._spool_path(), "w", encoding="utf-8") as f:
        f.write(original)
    assert store.drain_fleet_spool(store) == 3
    assert stored() == [("worker", n, str(n)) for n in range(3)]


def test_failed_commit_keeps_spool_for_retry(isolated_store, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(store, "_db", lambda **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
        assert store.record_fleet_turn("worker", {"turn": 1, "text": "hello"}) is False
    original_db = store._open_spool_db

    class FailedCommit:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def commit(self):
            raise sqlite3.OperationalError("commit failed")

    monkeypatch.setattr(store, "_open_spool_db", lambda module: FailedCommit(original_db(module)))
    with pytest.raises(sqlite3.OperationalError, match="commit failed"):
        store.drain_fleet_spool(store)
    assert open(store._spool_path(), encoding="utf-8").read()
    monkeypatch.setattr(store, "_open_spool_db", original_db)
    assert store.drain_fleet_spool(store) == 1
    assert stored() == [("worker", 1, "hello")]


def test_record_does_not_wait_for_database_writer(isolated_store):
    conn = sqlite3.connect(store._db_path(), isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        start = time.perf_counter()
        assert store.record_fleet_turn("worker", {"turn": 1}) is False
        assert time.perf_counter() - start < 0.5
        assert open(store._spool_path(), encoding="utf-8").read()
    finally:
        conn.rollback()
        conn.close()

    assert store.drain_fleet_spool(store) == 1
    assert store.drain_fleet_spool(store) == 0
    assert stored() == [("worker", 1, "")]
