# -*- coding: utf-8 -*-
"""Acceptance checks own their whole subprocess tree until commit/close."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
import relay.acceptance as acceptance
from pathlib import Path

from relay.acceptance import Check
from relay.relay_fleet import RelayWorker
from tools import childproc


def _alive(pid: int) -> bool:
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           creationflags=childproc.headless_creationflags())
        return str(pid).encode() in (r.stdout or b"")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = Path("/proc/%d/stat" % pid)
    if stat.is_file():
        try:
            fields = stat.read_text().split()
            if len(fields) > 2 and fields[2] == "Z":
                return False
        except Exception:
            pass
    return True


def _wait_dead(pid: int, seconds: float = 8.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def _tree_script(tmp_path: Path) -> Path:
    p = tmp_path / "tree_parent.py"
    marker = tmp_path / "grandchild.pid"
    code = r'''
import subprocess, sys, time
marker = sys.argv[1]
child = subprocess.Popen([sys.executable, "-c", "import os,time,sys;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(120)", marker])
time.sleep(120)
'''
    p.write_text(code, encoding="utf-8")
    return p


def _started_check(tmp_path: Path):
    marker = tmp_path / "grandchild.pid"
    c = Check({"type": "shell", "argv": [sys.executable, str(_tree_script(tmp_path)), str(marker)],
               "timeout": 60}, cwd=str(tmp_path)).start()
    end = time.time() + 8
    while time.time() < end and not marker.is_file():
        time.sleep(0.05)
    assert marker.is_file(), "grandchild never started; test proves nothing"
    return c, int(marker.read_text().strip())


def test_acceptance_timeout_kills_the_grandchild(tmp_path):
    c, grandchild = _started_check(tmp_path)
    c._deadline = time.time() - 1
    result = c.poll()
    assert result is not None and result[0] is False and "TIMEOUT" in result[1]
    assert _wait_dead(grandchild), "acceptance timeout left its grandchild alive"


def test_acceptance_cancel_kills_the_grandchild(tmp_path):
    c, grandchild = _started_check(tmp_path)
    result = c.cancel()
    assert result is not None and result[0] is False and "CANCELLED" in result[1]
    assert _wait_dead(grandchild), "acceptance cancel left its grandchild alive"
    assert c.poll() == result


def test_worker_close_cancels_an_active_acceptance_check():
    class FakeCheck:
        def __init__(self): self.calls = 0
        def cancel(self): self.calls += 1; return (False, "cancelled")

    w = object.__new__(RelayWorker)
    w.closed = False
    w._active_check = FakeCheck()
    w._pending_checks = [{"type": "pytest"}]
    w.socket = False
    w.drv = None
    w.page = None
    w._refuter_session = None
    w._research_session = None
    # Fields used by the best-effort accounting block.
    w.name = "unit"; w.goal = "g"; w.turn = 1; w.outcome = None; w.status = "verifying"
    w.reason = ""; w.jid = ""
    check = w._active_check
    w.close()
    assert check.calls == 1
    assert w._active_check is None
    assert w._pending_checks == []



def test_blocking_check_kills_tree_on_keyboardinterrupt(tmp_path, monkeypatch):
    real, grandchild = _started_check(tmp_path)

    class InterruptingCheck:
        def start(self):
            return self
        def poll(self):
            raise KeyboardInterrupt("unit interrupt")
        def cancel(self):
            return real.cancel()

    monkeypatch.setattr(acceptance, "Check", lambda *args, **kwargs: InterruptingCheck())
    with pytest.raises(KeyboardInterrupt, match="unit interrupt"):
        acceptance.run_check_blocking({"type": "shell", "argv": [sys.executable, "-c", "pass"]},
                                      cwd=str(tmp_path), poll_s=0.01)
    assert _wait_dead(grandchild), "blocking acceptance interrupt left its grandchild alive"
