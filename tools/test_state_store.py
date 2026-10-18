"""Unit tests for tools/state_store.py (atomic write, lock, backups, shrink guard).

Hermetic: everything lives under tmp_path. Run: pytest -q tools\test_state_store.py
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from tools import state_store as ss


def _valid(d):
    return isinstance(d, dict) and isinstance(d.get("e"), dict)


def _count(d):
    return len(d["e"])


def _upd(path, mutate, **kw):
    return ss.update_json(path, mutate, validate=_valid, count=_count,
                          default=lambda: {"e": {}}, **kw)


def test_roundtrip_keeps_format_and_leaves_no_temp(tmp_path):
    p = tmp_path / "s.json"
    _upd(p, lambda s: s["e"].update({"日本語": 1}))
    assert json.loads(p.read_text(encoding="utf-8")) == {"e": {"日本語": 1}}
    assert "日本語" in p.read_text(encoding="utf-8")  # ensure_ascii=False
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp") or f.endswith(".lock")]


def test_backup_generations_rotate_and_are_capped(tmp_path):
    p = tmp_path / "s.json"
    for i in range(ss.BACKUP_GENERATIONS + 3):
        _upd(p, lambda s, i=i: s["e"].update({f"k{i}": i}))
    baks = sorted(f for f in os.listdir(tmp_path) if ".bak." in f)
    assert len(baks) == ss.BACKUP_GENERATIONS
    newest = json.loads((tmp_path / "s.json.bak.1").read_text(encoding="utf-8"))
    assert newest == json.loads(p.read_text(encoding="utf-8"))
    oldest = json.loads((tmp_path / f"s.json.bak.{ss.BACKUP_GENERATIONS}").read_text(encoding="utf-8"))
    assert len(oldest["e"]) == len(newest["e"]) - (ss.BACKUP_GENERATIONS - 1)


def test_unreadable_without_backup_raises_and_leaves_bytes(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{ torn", encoding="utf-8")
    with pytest.raises(ss.StateUnreadable):
        ss.read_json(p, validate=_valid, default=lambda: {"e": {}})
    with pytest.raises(ss.StateUnreadable):
        _upd(p, lambda s: s["e"].update({"a": 1}))
    assert p.read_text(encoding="utf-8") == "{ torn"


def test_missing_and_zero_byte_without_backup_are_empty(tmp_path):
    assert ss.read_json(tmp_path / "nope.json", validate=_valid, default=lambda: {"e": {}}) == {"e": {}}
    z = tmp_path / "z.json"
    z.write_text("", encoding="utf-8")
    assert ss.read_json(z, validate=_valid, default=lambda: {"e": {}}) == {"e": {}}


def test_bom_file_still_loads(tmp_path):
    p = tmp_path / "s.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"e": {"a": 1}}).encode())
    assert ss.read_json(p, validate=_valid, default=lambda: {"e": {}}) == {"e": {"a": 1}}


def test_shrink_guard_refuses_big_drop_but_allows_small_and_explicit(tmp_path):
    p = tmp_path / "s.json"
    _upd(p, lambda s: s["e"].update({f"k{i}": i for i in range(100)}))
    with pytest.raises(ss.StateShrinkRefused):
        _upd(p, lambda s: [s["e"].pop(f"k{i}") for i in range(50)])
    assert len(json.loads(p.read_text(encoding="utf-8"))["e"]) == 100
    _upd(p, lambda s: s["e"].pop("k1"))  # one fewer: fine
    _upd(p, lambda s: [s["e"].pop(f"k{i}") for i in range(2, 60)], allow_shrink=True)
    assert len(json.loads(p.read_text(encoding="utf-8"))["e"]) == 41


def test_mutate_exception_writes_nothing(tmp_path):
    p = tmp_path / "s.json"
    _upd(p, lambda s: s["e"].update({"a": 1}))
    before = p.read_bytes()

    def boom(s):
        s["e"].clear()
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        _upd(p, boom)
    assert p.read_bytes() == before
    assert not (tmp_path / "s.json.lock").exists()


def test_lock_is_exclusive_and_times_out_instead_of_hanging(tmp_path):
    p = tmp_path / "s.json"
    with ss.file_lock(p):
        t0 = time.monotonic()
        with pytest.raises(ss.StateLockTimeout):
            with ss.file_lock(p, wait_seconds=0.3):
                pass
        assert time.monotonic() - t0 < 5
    with ss.file_lock(p, wait_seconds=0.3):  # released
        pass


def test_stale_lock_of_dead_pid_is_taken_over(tmp_path):
    p = tmp_path / "s.json"
    (tmp_path / "s.json.lock").write_text("999999 1 0", encoding="utf-8")
    assert not ss._pid_alive(999999)
    with ss.file_lock(p, wait_seconds=2):
        pass


def test_old_lock_of_live_pid_is_taken_over(tmp_path):
    p = tmp_path / "s.json"
    lock = tmp_path / "s.json.lock"
    lock.write_text(f"{os.getpid()} 1 0", encoding="utf-8")
    old = time.time() - 1000
    os.utime(lock, (old, old))
    with ss.file_lock(p, wait_seconds=2, stale_seconds=60):
        pass


def test_live_fresh_lock_is_respected(tmp_path):
    p = tmp_path / "s.json"
    (tmp_path / "s.json.lock").write_text(f"{os.getpid()} 1 0", encoding="utf-8")
    with pytest.raises(ss.StateLockTimeout):
        with ss.file_lock(p, wait_seconds=0.3):
            pass


def test_threads_do_not_lose_updates(tmp_path):
    p = tmp_path / "s.json"
    errs = []

    def work(w):
        try:
            for i in range(10):
                _upd(p, lambda s: s["e"].update({f"{w}-{i}": 1}))
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=work, args=(w,)) for w in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    assert len(json.loads(p.read_text(encoding="utf-8"))["e"]) == 40
