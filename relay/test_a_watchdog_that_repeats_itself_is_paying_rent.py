# -*- coding: utf-8 -*-
"""default_notify's toast watchdog: keep the watch, stop re-deriving the answer.

WHY IT EXISTS AT ALL. Commit e658a43 (2026-07-04), "Add self-identifying diagnostic to
default_notify (catches any future false toast)" -- so a freshly-spawned fleet names itself as
the emitter instead of anyone guessing from process trees. That is worth keeping and this file
does not remove it.

WHAT WAS WRONG. It wrote an eight-frame stack on EVERY toast, with no bound. Measured
2026-09-18: .fleet/_notify_source.log held 20,622 lines and 1.5 MB, and across all of it there
were FOUR distinct titles and SIXTEEN distinct stack frames. "default_notify <- run_relay_fleet
<- main" appeared 1,340 times. The answer space was small, completely enumerated, and being
re-derived on every single toast.

Not merely wasteful on this machine. tools/tool_ledger bounds itself and says why in its own
words -- "a ledger cannot become the thing that fills the disk, which on this machine is the
binding constraint, and has already stopped a benchmark run once" -- and this file had no bound
at all. A disk reaching zero here has truncated source files mid-write.

WHAT MUST STILL HOLD. A new emitter is the entire point, so a title or a call path never seen
before is still written in full the moment it appears. Only the repetition stops.
"""
from __future__ import annotations

import io
import os

import pytest

import relay.copilot_autopilot_relay as R


@pytest.fixture()
def notify_log(tmp_path, monkeypatch):
    """Point the watchdog at a temp file and give it a fresh seen-set."""
    log = tmp_path / "_notify_source.log"
    monkeypatch.setattr(R, "NOTIFY_SOURCE_LOG", str(log))
    monkeypatch.setattr(R, "_NOTIFY_SEEN", set())
    return log


def _lines(p):
    return io.open(str(p), encoding="utf-8").read() if os.path.exists(str(p)) else ""


def test_a_new_emitter_is_recorded_in_full(notify_log):
    """THE CAPABILITY. This is what the watchdog is for and it must not be weakened."""
    R._record_notify_source("並列自律フリート 完了")
    body = _lines(notify_log)
    assert "並列自律フリート 完了" in body
    assert "pid=" in body and "argv=" in body
    assert "File " in body, "the caller stack is gone; the emitter can no longer be identified"


def test_the_same_emitter_is_not_recorded_again(notify_log):
    """THE DEFECT. 1,340 copies of one answer."""
    for _ in range(50):
        R._record_notify_source("並列自律フリート 完了")
    body = _lines(notify_log)
    assert body.count("pid=") == 1, "the watchdog wrote %d records for one emitter" % body.count("pid=")


def test_a_different_title_from_the_same_place_is_still_new(notify_log):
    """A new kind of toast from a known call path is exactly the thing worth seeing."""
    R._record_notify_source("並列自律フリート 完了")
    R._record_notify_source("⚠ エージェントが停止/無効化されている可能性")
    assert _lines(notify_log).count("pid=") == 2


def test_the_file_is_bounded(notify_log, monkeypatch):
    """Unbounded was the other half. The cap keeps the RECENT half, because a watchdog is asked
    about what just appeared."""
    monkeypatch.setattr(R, "_NOTIFY_LOG_MAX_BYTES", 2000)
    notify_log.write_text("x" * 5000 + "\n", encoding="utf-8")
    R._record_notify_source("新しい発信元")
    assert notify_log.stat().st_size <= 5000, "the file was not trimmed"
    assert "新しい発信元" in _lines(notify_log), "the new record was lost to the trim"


def test_the_watchdog_never_raises_into_the_control_loop(notify_log, monkeypatch):
    """Its docstring promises this, and it runs on the path that tells a person what happened."""
    monkeypatch.setattr(R, "NOTIFY_SOURCE_LOG", os.path.join("no:", "such", "place", "x.log"))
    R._record_notify_source("title")     # must not raise
