# -*- coding: utf-8 -*-
"""A full C: must never kill a process through SQLite's memory-mapped -shm file.

MEASURED 2026-10-22: the fleet coordinator died three times (Application log Id 1000/1005,
17:36:01, 17:37:25, 17:37:30) with exception 0xc0000006 (in-page I/O error) inside sqlite3.dll
while touching sessions.sqlite3-shm / jobs.sqlite3-shm. WAL maps that file into the process; a
full disk turns a touch of the mapping into an OS exception Python cannot catch.

These tests need no full disk. They ask the operating system, for the real store-opening code,
which files this process has MAPPED (Windows: VirtualQuery + GetMappedFileNameW; Linux:
/proc/self/maps), and require that no <db>-shm is among them and that no -shm file exists.
test_a_plain_wal_connection_is_detected proves the probe itself sees the mapping, so a pass is
not an artefact of a blind probe. On the old code (journal_mode=WAL in all three stores) the
store tests fail.
"""
import os
import sqlite3
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bridge import session_store as S  # noqa: E402
from relay.local_job_store import LocalJobStore  # noqa: E402
from relay.skills import SkillStore  # noqa: E402
from relay.sqlite_journal import use_rollback_journal  # noqa: E402


def _mapped_shm_files():
    """Basenames of every *-shm file mapped into this process, or None if unknowable here."""
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wt

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ps = ctypes.WinDLL("psapi", use_last_error=True)

        class MBI(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                        ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]

        k32.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
        ps.GetMappedFileNameW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]
        k32.GetCurrentProcess.restype = wt.HANDLE
        mem_mapped = 0x40000
        proc = k32.GetCurrentProcess()
        addr, out = 0, []
        mbi = MBI()
        buf = ctypes.create_unicode_buffer(1024)
        while addr < 0x7FFFFFFF0000:
            if not k32.VirtualQuery(ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            if mbi.Type == mem_mapped and ps.GetMappedFileNameW(proc, ctypes.c_void_p(addr), buf, 1024):
                if buf.value.endswith("-shm"):
                    out.append(buf.value.rsplit("\\", 1)[-1])
            addr = (mbi.BaseAddress or 0) + mbi.RegionSize
        return out
    maps = "/proc/self/maps"
    if os.path.exists(maps):
        with open(maps, encoding="utf-8", errors="replace") as fh:
            return [ln.split()[-1].rsplit("/", 1)[-1] for ln in fh if ln.rstrip().endswith("-shm")]
    return None


@pytest.fixture(autouse=True)
def _probe_works():
    if _mapped_shm_files() is None:
        pytest.skip("no way to list memory-mapped files on this platform")


def _assert_no_shm(db_path):
    base = os.path.basename(db_path)
    assert not os.path.exists(db_path + "-shm"), "a -shm file exists for " + base
    assert (base + "-shm") not in _mapped_shm_files(), base + "-shm is memory-mapped"
    c = sqlite3.connect(db_path)
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    finally:
        c.close()


def test_a_plain_wal_connection_is_detected(tmp_path):
    """The probe must see the mapping WAL makes, or every other test here proves nothing."""
    p = str(tmp_path / "plain.sqlite3")
    c = sqlite3.connect(p, isolation_level=None)
    try:
        assert c.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        c.execute("CREATE TABLE t(x)")
        c.execute("INSERT INTO t VALUES (1)")
        c.execute("SELECT * FROM t").fetchall()
        assert "plain.sqlite3-shm" in _mapped_shm_files()
    finally:
        c.close()


def test_the_session_store_maps_no_shm(tmp_path, monkeypatch):
    monkeypatch.setenv(S.STORE_DIR_ENV, str(tmp_path))
    sess = S.new_session(title="x")
    S.append_turn(sess["sid"], "user", "hello")
    assert [t["text"] for t in S.all_turns(sess["sid"])] == ["hello"]
    _assert_no_shm(S._db_path())


def test_the_job_store_maps_no_shm(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    conn = store._connect()
    try:
        conn.execute("SELECT count(*) FROM jobs").fetchone()
    finally:
        conn.close()
    _assert_no_shm(str(store.path))


def test_the_skill_store_maps_no_shm(tmp_path):
    root = tmp_path / "project"
    (root / "skills").mkdir(parents=True)
    store = SkillStore(str(root), db_path=tmp_path / "skills.sqlite3")
    with store._connect() as db:
        db.execute("SELECT count(*) FROM skill_sources").fetchone()
    _assert_no_shm(str(store.db_path))


def test_an_existing_wal_database_is_converted_when_nobody_else_has_it(tmp_path):
    """Production files are already in WAL (persistent in the file); they must leave it."""
    p = str(tmp_path / "old.sqlite3")
    c = sqlite3.connect(p, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(x)")
    c.execute("INSERT INTO t VALUES (7)")
    c.close()
    c = sqlite3.connect(p, isolation_level=None)
    try:
        assert use_rollback_journal(c) == "delete"
        assert c.execute("SELECT x FROM t").fetchone()[0] == 7
    finally:
        c.close()
    _assert_no_shm(p)


def test_a_wal_database_held_by_another_connection_is_left_alone_without_stalling(tmp_path):
    """The conversion needs the only connection. When another holds the file the open must
    return at once with the old mode, not raise and not wait out the busy timeout."""
    p = str(tmp_path / "held.sqlite3")
    holder = sqlite3.connect(p, isolation_level=None)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("CREATE TABLE t(x)")
        holder.execute("INSERT INTO t VALUES (1)")
        c = sqlite3.connect(p, isolation_level=None, timeout=10)
        try:
            t0 = time.monotonic()
            mode = use_rollback_journal(c)
            elapsed = time.monotonic() - t0
            assert mode == "wal"
            # The busy timeout in force was 10 s; returning far sooner shows it was bypassed.
            assert elapsed < 3, "the conversion attempt waited %.1f s" % elapsed
            assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        finally:
            c.close()
    finally:
        holder.close()
    c = sqlite3.connect(p, isolation_level=None)
    try:
        assert use_rollback_journal(c) == "delete"  # once alone, the next open converts it
    finally:
        c.close()
