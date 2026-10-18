# -*- coding: utf-8 -*-
"""The bridge binds its port BEFORE it prunes the session store.

start_all.ps1 polls http://127.0.0.1:8765/conv for 8 x 2 s and calls a bridge that has not
answered by then a failed start. /conv is an open route (no token, no page, no conversation), so
it answers the moment the server listens -- but main() ran S.apply_retention() first, and on a
345 MB store that pass took 2 s (14 sessions removed) to 17.6 s (540 removed), measured on a copy
of the real store 2026-10-18. A slow prune was reported as a broken start.

These tests RUN main() (page executor, watchdog and tool probe stubbed, so no browser) with a
retention pass that blocks until the test lets it go.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture()
def running_main(tmp_path, monkeypatch):
    """main() on a thread with everything that would touch a browser stubbed out."""
    monkeypatch.setenv("MCP_SESSION_STORE_DIR", str(tmp_path / "sessions"))
    from bridge import bridge_auth
    monkeypatch.setenv(bridge_auth.TOKEN_DIR_ENV, str(tmp_path / "token"))
    import bridge.copilot_bridge as B

    port = _free_port()
    monkeypatch.setenv("MCP_BRIDGE_PORT", str(port))
    monkeypatch.setattr(B, "BRIDGE_TOKEN", None)
    monkeypatch.setattr(B, "_RETENTION_DONE", False, raising=False)
    monkeypatch.setattr(B, "_configure_logging", lambda: None)
    monkeypatch.setattr(B, "_port_already_served", lambda p, timeout=2.0: False)
    monkeypatch.setattr(B, "_start_cdp_watchdog", lambda cdp: None)
    monkeypatch.setattr(B, "_schedule_tool_probe", lambda delay=None: None)

    class _NoPage:                       # PageExecutor.start() returns at once; so does this
        def start(self, fn):
            pass
    monkeypatch.setattr(B, "PAGE_EXECUTOR", _NoPage())

    servers = []

    class _Capturing(B._SingleBindHTTPServer):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            servers.append(self)
    monkeypatch.setattr(B, "_SingleBindHTTPServer", _Capturing)

    rec = _Records()
    B.logger.addHandler(rec)
    old_level = B.logger.level
    B.logger.setLevel(logging.DEBUG)

    t = threading.Thread(target=B.main, daemon=True)
    try:
        yield B, port, t, rec
    finally:
        for s in servers:
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
        t.join(timeout=5)
        B.logger.removeHandler(rec)
        B.logger.setLevel(old_level)


def _conv_answers(port, deadline_s):
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/conv" % port, timeout=1) as r:
                return r.status == 200
        except Exception:
            time.sleep(0.05)
    return False


def test_conv_answers_while_retention_is_still_running(running_main, monkeypatch):
    B, port, t, rec = running_main
    started, release = threading.Event(), threading.Event()

    def slow_retention(now=None, protect=None):
        started.set()
        release.wait(30)
        return {"removed_sessions": 3, "mb_after": 1.0}

    monkeypatch.setattr(B.S, "apply_retention", slow_retention)
    t.start()
    try:
        assert _conv_answers(port, 5), (
            "/conv did not answer within 5 s although the only thing standing in the way was a "
            "retention pass -- the bridge prunes before it binds")
        assert not release.is_set()
        assert started.wait(5), "retention never started"
        assert not any("session retention took" in m for m in rec.lines), (
            "retention finished before it was released; the test proves nothing")
    finally:
        release.set()
    deadline = time.time() + 5
    while time.time() < deadline and not any("session retention took" in m for m in rec.lines):
        time.sleep(0.05)
    assert any("session retention took" in m for m in rec.lines)
    assert any(m.startswith("session retention: removed 3 session(s)") for m in rec.lines)
    listening = [i for i, m in enumerate(rec.lines) if m.startswith("listening on 127.0.0.1:")]
    took = [i for i, m in enumerate(rec.lines) if m.startswith("session retention took")]
    assert listening and took and listening[0] < took[0], (
        "the 'listening' line must be on disk before the retention timing line: %r" % rec.lines)


def test_retention_runs_exactly_once_and_never_overlaps_itself(running_main, monkeypatch):
    B, port, t, rec = running_main
    calls, inside, overlap = [], [0], [False]
    gate = threading.Event()

    def counted(now=None, protect=None):
        calls.append(1)
        inside[0] += 1
        overlap[0] = overlap[0] or inside[0] > 1
        gate.wait(5)
        inside[0] -= 1
        return None

    monkeypatch.setattr(B.S, "apply_retention", counted)
    t.start()
    assert _conv_answers(port, 5)
    # a second request for a pass while the first is in flight, and one after it finished
    assert B._run_session_retention_once() is False
    gate.set()
    deadline = time.time() + 5
    while time.time() < deadline and not any("session retention took" in m for m in rec.lines):
        time.sleep(0.05)
    assert B._run_session_retention_once() is False
    assert len(calls) == 1 and not overlap[0]


def _old_and_fresh(S):
    old = S.new_session(title="old")
    S.append_turn(old["sid"], "user", "ancient")
    S.touch(old["sid"], last_active_ts=1000.0)
    fresh = S.new_session(title="fresh")
    S.append_turn(fresh["sid"], "user", "recent")
    return old["sid"], fresh["sid"]


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SESSION_STORE_DIR", str(tmp_path / "sessions"))
    from bridge import session_store as S
    monkeypatch.setattr(S, "_settings_path", lambda: str(tmp_path / "settings.txt"))
    (tmp_path / "settings.txt").write_text("maxtabs=3\n", encoding="utf-8")   # 90-day default
    assert S._base_dir() == str(tmp_path / "sessions")
    return S


def test_the_startup_pass_still_removes_what_it_removed_before(isolated_store, monkeypatch):
    import bridge.copilot_bridge as B
    S = isolated_store
    old, fresh = _old_and_fresh(S)
    monkeypatch.setattr(B, "_RETENTION_DONE", False, raising=False)
    monkeypatch.setattr(B, "ACTIVE_SID", None)
    assert B._run_session_retention_once() is True
    assert S.load(old) is None, "the over-age session was not removed"
    assert S.load(fresh) is not None and len(S.all_turns(fresh)) == 1


def test_the_session_the_server_is_driving_is_not_removed(isolated_store, monkeypatch):
    import bridge.copilot_bridge as B
    S = isolated_store
    old, _fresh = _old_and_fresh(S)
    monkeypatch.setattr(B, "_RETENTION_DONE", False, raising=False)
    monkeypatch.setattr(B, "ACTIVE_SID", old)       # an old conversation the bridge resumed
    B._run_session_retention_once()
    assert S.load(old) is not None and len(S.all_turns(old)) == 1


def test_a_session_touched_after_it_was_selected_is_not_removed(isolated_store):
    S = isolated_store
    old, _fresh = _old_and_fresh(S)

    def touched_meanwhile():
        S.touch(old)                      # a request resumes it after the pass selected it
        return ()

    out = S.apply_retention(protect=touched_meanwhile)
    assert out["removed_sessions"] == 0
    assert S.load(old) is not None and len(S.all_turns(old)) == 1
