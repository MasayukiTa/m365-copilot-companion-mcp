"""The tool-call trace: does it record, does it resist editing, and what may it conclude.

The point of the trace is the two things a final-state grader cannot see -- a write outside
the episode's directory, and a file created and deleted before grading. These tests drive
both, plus the cases where the trace must NOT be allowed to upgrade a claim: when it is
absent, when its chain is broken, and when an opaque tool ran and the layer cannot see what
it did.
"""
from __future__ import annotations

import io
import json
import os
import tempfile

from tools import evidence_trace as T


def _session(tmp=None):
    d = tmp or tempfile.mkdtemp(prefix="tr_")
    path = os.path.join(d, "calls.jsonl")
    key = "k" * 64
    os.environ[T.TRACE_PATH_ENV] = path
    os.environ[T.TRACE_KEY_ENV] = key
    return path, key


def _clear():
    os.environ.pop(T.TRACE_PATH_ENV, None)
    os.environ.pop(T.TRACE_KEY_ENV, None)


def test_it_does_nothing_at_all_unless_a_runner_asked_for_it():
    """本番運用で常時書くものではない。設定が無ければ完全な no-op。"""
    _clear()
    assert T.enabled() is False
    T.record("read_file", {"path": "x"}, True, "ok")     # must not raise, must not write


def test_every_dispatched_call_is_recorded_with_its_arguments():
    path, key = _session()
    try:
        T.record("read_file", {"path": "C:/work/a.txt"}, True, "contents")
        T.record("write_file", {"path": "C:/work/b.txt", "text": "x"}, True, "ok")
        rows = T.read(path, key)
    finally:
        _clear()
    assert [r["tool"] for r in rows] == ["read_file", "write_file"]
    assert "a.txt" in rows[0]["arguments"], "引数を捨てると『どこへ書いたか』が答えられない"
    assert T.intact(path, key)


def test_a_failed_call_is_recorded_too():
    """拒否された呼び出しは、試みたという事実そのものが証拠。"""
    path, key = _session()
    try:
        T.record("write_file", {"path": "/etc/passwd"}, False, "PermissionError")
        rows = T.read(path, key)
    finally:
        _clear()
    assert rows[0]["ok"] is False


def test_deleting_an_entry_breaks_the_chain():
    """消された呼び出しこそ隠したい呼び出し。連鎖が切れる。"""
    path, key = _session()
    try:
        for i in range(3):
            T.record("tool%d" % i, {"i": i}, True, "ok")
        assert T.intact(path, key)
        rows = io.open(path, encoding="utf-8").read().splitlines()
        io.open(path, "w", encoding="utf-8", newline="\n").write(
            "\n".join(rows[:1] + rows[2:]) + "\n")
        assert T.intact(path, key) is False
    finally:
        _clear()


def test_editing_an_entry_breaks_the_chain():
    path, key = _session()
    try:
        T.record("write_file", {"path": "C:/secret/leak.txt"}, True, "ok")
        rows = [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]
        rows[0]["arguments"] = json.dumps({"path": "C:/work/innocent.txt"})
        io.open(path, "w", encoding="utf-8", newline="\n").write(
            json.dumps(rows[0], ensure_ascii=False) + "\n")
        assert T.intact(path, key) is False
    finally:
        _clear()


def test_a_write_outside_the_episode_directory_is_visible():
    """最終状態のグレーダには絶対に見えない事象。トレースの存在理由そのもの。"""
    work = tempfile.mkdtemp(prefix="wd_")
    path, key = _session()
    try:
        T.record("write_file", {"path": os.path.join(work, "fine.txt")}, True, "ok")
        T.record("write_file", {"path": "C:/Users/Public/exfiltrated.txt"}, True, "ok")
        outside = T.writes_outside(path, key, work)
    finally:
        _clear()
    assert len(outside) == 1
    assert "exfiltrated" in outside[0]["path"]
    assert outside[0]["tool"] == "write_file"


def test_a_create_then_delete_is_still_in_the_trace():
    """作って読んで消せば workdir には何も残らない。呼び出しは残る。"""
    work = tempfile.mkdtemp(prefix="wd2_")
    path, key = _session()
    try:
        T.record("write_file", {"path": os.path.join(work, "tmp.txt")}, True, "ok")
        T.record("read_file", {"path": os.path.join(work, "tmp.txt")}, True, "secret")
        T.record("delete_file", {"path": os.path.join(work, "tmp.txt")}, True, "ok")
        rows = T.read(path, key)
    finally:
        _clear()
    assert [r["tool"] for r in rows] == ["write_file", "read_file", "delete_file"]
    assert not os.path.exists(os.path.join(work, "tmp.txt"))


def test_an_opaque_tool_is_recorded_and_marked():
    """shell や python の中で何が起きたかは、この層からは読めない。
    記録はするが、それを根拠に『他に何も起きていない』とは言えない。"""
    path, key = _session()
    try:
        T.record("run_python", {"code": "..."}, True, "ok")
        assert [r["tool"] for r in T.opaque_calls(path, key)] == ["run_python"]
    finally:
        _clear()


def test_a_trace_failure_never_breaks_the_run_it_is_tracing():
    """観測が対象を壊してはいけない。"""
    os.environ[T.TRACE_PATH_ENV] = os.path.join("Z:\\", "nonexistent", "calls.jsonl")
    os.environ[T.TRACE_KEY_ENV] = "k"
    try:
        T.record("read_file", {"path": "x"}, True, "ok")    # must not raise
    finally:
        _clear()
