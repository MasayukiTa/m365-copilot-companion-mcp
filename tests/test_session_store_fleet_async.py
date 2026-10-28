import sqlite3
import threading

import pytest

from bridge import session_store as store


@pytest.fixture(scope="module", autouse=True)
def isolated_store(tmp_path_factory):
    from pytest import MonkeyPatch
    patch = MonkeyPatch()
    patch.setenv(store.STORE_DIR_ENV, str(tmp_path_factory.mktemp("fleet-store")))
    yield
    assert store.flush_fleet_turns(10)
    patch.undo()


def test_order_and_flush():
    def produce(key):
        for n in range(40):
            store.record_fleet_turn_async(key, {"turn": n, "text": str(n)})

    threads = [threading.Thread(target=produce, args=(key,)) for key in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.flush_fleet_turns(10)
    conn = store._db(import_files=False)
    try:
        for key in ("a", "b"):
            rows = conn.execute("SELECT turn FROM fleet_turns WHERE key = ? ORDER BY rowid",
                                (key,)).fetchall()
            assert [row[0] for row in rows] == list(range(40))
    finally:
        conn.close()


def test_slow_writer_does_not_block_producer_and_flush_waits(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = store._write_fleet_turn_batch

    def slow(conn, batch):
        entered.set()
        assert release.wait(10)
        original(conn, batch)

    monkeypatch.setattr(store, "_write_fleet_turn_batch", slow)
    try:
        store.record_fleet_turn_async("slow", {"turn": 0})
        assert entered.wait(10)
        producer = threading.Thread(target=lambda: [store.record_fleet_turn_async(
            "slow", {"turn": n}) for n in range(1, 300)])
        producer.start()
        producer.join(2)
        assert not producer.is_alive()
        assert not store.flush_fleet_turns(0.01)
    finally:
        release.set()
        assert store.flush_fleet_turns(10)


def test_sqlite_error_is_logged_and_writer_continues(monkeypatch, caplog):
    original = store._write_fleet_turn_batch
    called = threading.Event()

    def fail_once(conn, batch):
        if not called.is_set():
            called.set()
            raise sqlite3.OperationalError("test failure")
        original(conn, batch)

    monkeypatch.setattr(store, "_write_fleet_turn_batch", fail_once)
    store.record_fleet_turn_async("error", {"turn": 1})
    assert store.flush_fleet_turns(10)
    assert "test failure" in caplog.text
    store.record_fleet_turn_async("recovered", {"turn": 2})
    assert store.flush_fleet_turns(10)
    conn = store._db(import_files=False)
    try:
        assert conn.execute("SELECT turn FROM fleet_turns WHERE key = ?", ("recovered",)).fetchone()[0] == 2
    finally:
        conn.close()
