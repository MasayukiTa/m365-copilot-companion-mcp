"""run_python through the pre-started interpreter pool behaves like the plain path.

The pool hides the 1-2 s interpreter start-up (see tools/runpy_pool.py). These tests pin the
behaviour that must NOT change: output format, working_dir, exception exit code, timeout,
and the fall-back to a plain start when the pool is off or empty.
"""
from __future__ import annotations

import time

import pytest

from tools import code_exec, runpy_pool


@pytest.fixture(autouse=True)
def _pool_env(monkeypatch, tmp_path):
    monkeypatch.setattr(code_exec, "require_unlocked", lambda: None)
    monkeypatch.setattr(code_exec, "_judged", lambda *a, **k: None)
    monkeypatch.setattr(code_exec, "_working_dir", lambda p: str(p or tmp_path))
    monkeypatch.setenv("MCP_RUNPY_POOL", "2")
    runpy_pool.shutdown()
    yield
    runpy_pool.shutdown()


def _rp(code, **kw):
    return code_exec.run_python(code, **kw).replace("\r\n", "\n")


def _wait_pool(n=1, limit=60.0):
    runpy_pool.ensure_filling()
    t = time.time()
    while time.time() - t < limit:
        with runpy_pool._lock:
            live = [p for p in runpy_pool._pool if p.poll() is None]
        if len(live) >= n:
            return
        time.sleep(0.2)
    raise AssertionError("pool did not fill")


def test_pool_runs_print():
    _wait_pool()
    assert _rp("print(1)") == "[stdout]\n1\n"


def test_pool_exception_gives_returncode_and_stderr():
    _wait_pool()
    out = code_exec.run_python("raise ValueError('boom')")
    assert "[stderr]" in out and "ValueError: boom" in out
    assert out.rstrip().endswith("[returncode: 1]")


def test_pool_working_dir(tmp_path):
    _wait_pool()
    d = tmp_path / "sub"
    d.mkdir()
    out = code_exec.run_python("import os; print(os.getcwd())", working_dir=str(d))
    assert str(d) in out


def test_pool_timeout_stops_the_process():
    _wait_pool()
    t = time.time()
    out = code_exec.run_python("import time; time.sleep(30)", timeout=2)
    assert out == "[timeout: exceeded 2 seconds]"
    assert time.time() - t < 15


def test_empty_pool_and_pool_off_still_work(monkeypatch):
    runpy_pool.shutdown()
    monkeypatch.setenv("MCP_RUNPY_POOL", "0")
    assert _rp("print(2)") == "[stdout]\n2\n"
    monkeypatch.setenv("MCP_RUNPY_POOL", "2")
    runpy_pool.shutdown()
    assert _rp("print(3)") == "[stdout]\n3\n"  # first call: empty pool


def test_dead_warm_interpreter_falls_back():
    _wait_pool(1)
    with runpy_pool._lock:
        for p in runpy_pool._pool:
            p.kill()
            p.wait(timeout=10)
    assert _rp("print(4)") == "[stdout]\n4\n"


def test_stdin_is_closed_like_the_plain_path():
    _wait_pool()
    out = code_exec.run_python("import sys; print(repr(sys.stdin.read()))")
    assert "''" in out
