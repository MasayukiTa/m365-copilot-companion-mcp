# -*- coding: utf-8 -*-
"""The cockpit polls status.json. That must not be able to end a fleet run.

MEASURED 2026-09-17 10:21:52. A coordinator died before its first turn:

    File "relay/fleet_runner.py", line 1368, in _write_atomic
        os.replace(tmp, path)   # atomic on Windows + POSIX
    PermissionError: [WinError 5] .fleet\\status.json.tmp -> .fleet\\status.json

The comment was half right. os.replace IS atomic on both platforms, and on Windows it is also
REFUSED while another process holds the destination open without FILE_SHARE_DELETE. The
cockpit reads this file about once a second and holds it for a few milliseconds, so this is
not bad luck -- it is a collision with a reader that is always there, and the first status
write is the very first thing a run does.

The autostart record said the fleet had started. It had; it was already dead. That is a
separate defect with its own fix, and it is why this one went unnoticed: the only symptom was
a goal that never answered.

WHAT IS ASSERTED HERE. That a transient refusal is retried, that a permanent one still raises,
and that the retry is bounded -- a fleet that hangs forever writing its status is not an
improvement on one that dies.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fleet_runner as FR             # noqa: E402


def test_a_reader_holding_the_file_for_a_moment_costs_a_delay_not_a_run(tmp_path, monkeypatch):
    """Two refusals then success -- the shape of a poll that overlapped the write."""
    path = str(tmp_path / "status.json")
    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(FR.os, "replace", flaky)
    FR._write_atomic(path, {"running": True})
    assert calls["n"] == 3, "the write gave up or never retried"
    assert os.path.isfile(path)


def test_a_refusal_that_never_clears_is_still_a_failure(tmp_path, monkeypatch):
    """NOT SWALLOWED. A status file that genuinely cannot be written is a real failure: a
    fleet running with no status is a fleet the panel reports as dead, which is the misreport
    this repository has spent a week removing. The retry converts a lost race into a delay,
    never into a silent skip."""
    path = str(tmp_path / "status.json")

    def always(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(FR.os, "replace", always)
    monkeypatch.setattr(FR, "_REPLACE_DEADLINE_S", 0.15)
    with pytest.raises(PermissionError):
        FR._write_atomic(path, {"running": True})


def test_the_retry_is_bounded(tmp_path, monkeypatch):
    """A run that hangs forever writing its status is not an improvement on one that dies."""
    path = str(tmp_path / "status.json")
    monkeypatch.setattr(FR.os, "replace",
                        lambda s, d: (_ for _ in ()).throw(PermissionError(5, "denied")))
    monkeypatch.setattr(FR, "_REPLACE_DEADLINE_S", 0.2)
    started = FR.time.time()
    with pytest.raises(PermissionError):
        FR._write_atomic(path, {})
    assert FR.time.time() - started < 3.0, "the deadline did not bound the retry"


def test_other_errors_are_not_retried(tmp_path, monkeypatch):
    """A missing directory is not a reader holding a handle, and retrying it wastes the
    deadline before reporting the same thing."""
    path = str(tmp_path / "status.json")
    calls = {"n": 0}

    def gone(src, dst):
        calls["n"] += 1
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(FR.os, "replace", gone)
    with pytest.raises(OSError):
        FR._write_atomic(path, {})
    assert calls["n"] == 1, "a non-permission error was retried"
