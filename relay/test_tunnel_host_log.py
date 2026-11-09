# -*- coding: utf-8 -*-
"""The supervisor-owned host reader keeps draining, stamps, rotates and restarts.

Four properties, all with a FAKE command (sys.executable running a short -c program) so
no devtunnel and nothing Windows-only is touched -- the suite runs on the Linux CI runner:

* a fake host that writes ~5 MB quickly must NOT block while nobody consumes the pipe;
  run_once drains both streams on their own threads, so it returns and every byte lands
  in the log. This is the regression: the old starter left the pipe unread and the host
  blocked / its output was lost.
* every written line carries a millisecond local-time stamp.
* the log rotates at its size cap, keeping the configured number of old files, and keeps
  appending (survives the rotation).
* backoff_seconds grows per consecutive failure and caps; serve_forever restarts a
  fast-failing fake command with that backoff and logs each exit code, then stops at
  max_restarts (sleep patched so the test does not actually wait).
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time

import pytest

from relay import tunnel_host_log as thl

_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} ")


def test_millis_stamp_shape():
    s = thl.millis_stamp(1762000000.123456)
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$", s)
    # millisecond, not microsecond: exactly three fractional digits.
    assert s.split(".")[1] == "123"


def test_backoff_grows_and_caps():
    assert thl.backoff_seconds(1, base=2.0, cap=60.0) == 2.0
    assert thl.backoff_seconds(2, base=2.0, cap=60.0) == 4.0
    assert thl.backoff_seconds(3, base=2.0, cap=60.0) == 8.0
    assert thl.backoff_seconds(10, base=2.0, cap=60.0) == 60.0   # capped
    assert thl.backoff_seconds(0, base=2.0, cap=60.0) == 2.0     # floor at 1 failure


def test_a_fast_5mb_writer_is_not_blocked_while_nobody_reads(tmp_path):
    # The fake host prints ~5 MB to stdout as fast as it can. If the reader did NOT drain
    # the pipe, the child would block on a full OS pipe buffer and run_once would hang; the
    # test's own wall-clock guard would then fail. Because run_once drains on threads, the
    # child finishes and every line is in the log.
    log = tmp_path / "ts.log"
    sink = thl.RotatingLog(str(log), max_bytes=50 * 1024 * 1024, backups=3)
    # 5000 lines x ~1024 bytes ~= 5 MB. Line-oriented so the reader's readline loop applies.
    prog = (
        "import sys\n"
        "line = 'x' * 1000\n"
        "[sys.stdout.write(line + '\\n') for _ in range(5000)]\n"
        "sys.stdout.flush()\n"
    )
    start = time.time()
    rc, recycled = thl.run_once([sys.executable, "-c", prog], sink)
    elapsed = time.time() - start
    sink.close()
    assert rc == 0
    assert recycled is False  # no connection watcher was passed, so nothing recycled it
    assert elapsed < 30, "reader blocked: a 5 MB writer should drain quickly"
    written = log.read_text(encoding="utf-8", errors="replace").splitlines()
    # The startup line is not written by run_once (serve_forever writes that); here every
    # line is a stamped 'xxxx...' from the fake host. All 5000 must be present and stamped.
    payload = [l for l in written if "x" * 100 in l]
    assert len(payload) == 5000
    assert all(_STAMP.match(l) for l in payload)


def test_rotation_keeps_n_backups_and_appends(tmp_path):
    log = tmp_path / "ts.log"
    # Tiny cap so a handful of lines forces several rotations.
    sink = thl.RotatingLog(str(log), max_bytes=400, backups=3)
    for i in range(80):
        sink.write_line("line-%03d-%s" % (i, "y" * 30))
    sink.close()
    # The live file plus at most `backups` rotated files, never more.
    assert log.exists()
    rotated = [p for p in os.listdir(tmp_path) if p.startswith("ts.log.")]
    assert 1 <= len(rotated) <= 3
    assert not os.path.exists(str(log) + ".4")   # the oldest beyond the cap is discarded
    # Appending continues after rotation: the newest line is in the live file.
    assert "line-079" in log.read_text(encoding="utf-8")
    # Every rotated file is non-empty and its lines are stamped.
    for name in rotated:
        body = (tmp_path / name).read_text(encoding="utf-8").splitlines()
        assert body and all(_STAMP.match(l) for l in body)


def test_serve_forever_restarts_with_backoff_and_logs_exit_codes(tmp_path, monkeypatch):
    # A fake command that always exits fast with a known non-zero code. serve_forever must
    # run it, log the exit code, back off, and retry -- stopping at max_restarts. sleep is
    # patched so the backoff is recorded without the test actually waiting.
    log = tmp_path / "ts.log"
    slept = []
    monkeypatch.setattr(thl.time, "sleep", lambda s: slept.append(s))
    prog = "import sys; sys.exit(3)"
    fake = [sys.executable, "-c", prog]
    # Point serve_forever at our fake by overriding the command it builds: devtunnel bin is
    # the python exe and the args become `-v host <name>` -- but our fake ignores args and
    # just exits 3, which is all the restart loop needs to observe.
    real_run_once = thl.run_once
    monkeypatch.setattr(
        thl, "run_once",
        lambda cmd, sink, on_exit=None, hook=None, read_conn=None, grace=None, poll=None:
            real_run_once(fake, sink, hook=hook),
    )
    thl.serve_forever("any-tunnel", root=str(tmp_path), devtunnel=sys.executable,
                      max_bytes=10 * 1024 * 1024, backups=2, max_restarts=3,
                      watch_connections=False)
    # serve_forever writes to <root>/.setup/logs/devtunnel-host-ts.log, not <root>/ts.log.
    log = tmp_path / ".setup" / "logs" / "devtunnel-host-ts.log"
    body = log.read_text(encoding="utf-8")
    # Three runs, each logging the exit code.
    assert body.count("host exited code=3") == 3
    # Backoff was applied between restarts (two gaps for three runs), growing: 2 then 4.
    assert slept[:2] == [2.0, 4.0]


def test_serve_forever_survives_a_missing_binary(tmp_path, monkeypatch):
    # If the host binary cannot be started, the loop records it rather than crashing.
    log = tmp_path / "ts.log"
    monkeypatch.setattr(thl.time, "sleep", lambda s: None)

    def _boom(cmd, sink, on_exit=None, hook=None, read_conn=None, grace=None, poll=None):
        raise FileNotFoundError("devtunnel not found")

    monkeypatch.setattr(thl, "run_once", _boom)
    thl.serve_forever("any-tunnel", root=str(tmp_path), devtunnel="nope",
                      max_restarts=1, watch_connections=False)
    log = tmp_path / ".setup" / "logs" / "devtunnel-host-ts.log"
    body = log.read_text(encoding="utf-8")
    assert "cannot start host" in body
    assert "host exited code=127" in body


# -- SELF-RECOVERY WHEN THE RELAY STAYS AT ZERO CONNECTIONS -----------------------------------
# The 2026-11-04 03:46-03:53 incident: this PC's host process stayed alive with zero Host
# connections and nothing replaced it for 7 minutes, because the duplicate-launch guard must
# never start a second reader. The reader now notices this itself and ends ONLY its own host
# child so the existing restart loop brings up a fresh host -- still exactly one reader.

#: (seconds_at_zero, expected word) at the shared 240 s grace.
_RECYCLE_TABLE = [
    (None, "wait"),            # never zero / unreadable count -> never recycle
    (0, "wait"),               # just dropped to zero
    (149, "wait"),             # at the four-tick debounce, still inside grace
    (240, "wait"),             # exactly at the grace, not past it
    (240.0001, "recycle"),     # just past the grace
    (600, "recycle"),          # long wedged at zero
    ("not-a-number", "wait"),  # an unreadable duration must never recycle
]


@pytest.mark.parametrize("secs,expected", _RECYCLE_TABLE)
def test_decide_zero_conn_host_recycle_is_pure(secs, expected):
    assert thl.decide_zero_conn_host_recycle(secs) == expected


def test_the_reader_grace_matches_the_supervisor_grace():
    # The reader ends its host child from the inside; supervisor_zero_conn_recovery ends the
    # same child from the outside. The two limits are deliberately equal so neither fires
    # noticeably before the other. A change to one must change the other -- pinned here.
    # conftest.py puts the worktree root (not scripts/) at sys.path[0], so import the module
    # as scripts.supervisor_zero_conn_recovery rather than by its bare name.
    from scripts import supervisor_zero_conn_recovery as Z
    assert thl.ZERO_CONN_GRACE_SECONDS == Z.ZERO_CONN_GRACE_SECONDS


def test_a_custom_reader_grace_is_honoured():
    assert thl.decide_zero_conn_host_recycle(11, grace=10) == "recycle"
    assert thl.decide_zero_conn_host_recycle(9, grace=10) == "wait"
    # A bad grace falls back to the default, so a clearly-wedged streak still recycles.
    assert thl.decide_zero_conn_host_recycle(999, grace="nope") == "recycle"


def test_read_host_connections_parses_the_relay_line(monkeypatch):
    # The reader reads the SAME "Host connections : N" line the supervisor reads.
    class _P:
        def __init__(self, out):
            self.stdout = out
    sample = (b"Tunnel ID        : abc.use\r\n"
              b"Host connections : 3\r\n"
              b"Client connections : 0\r\n")
    monkeypatch.setattr(thl.subprocess, "run", lambda *a, **k: _P(sample))
    assert thl.read_host_connections("devtunnel", "any") == 3
    monkeypatch.setattr(thl.subprocess, "run", lambda *a, **k: _P(b"Host connections : 0\n"))
    assert thl.read_host_connections("devtunnel", "any") == 0
    # No such line -> None (cannot tell), which the decider treats as wait.
    monkeypatch.setattr(thl.subprocess, "run", lambda *a, **k: _P(b"offline\n"))
    assert thl.read_host_connections("devtunnel", "any") is None


def test_read_host_connections_is_none_when_the_cli_fails(monkeypatch):
    def _boom(*a, **k):
        raise OSError("devtunnel missing")
    monkeypatch.setattr(thl.subprocess, "run", _boom)
    assert thl.read_host_connections("devtunnel", "any") is None


class _FakeProc:
    """A stand-in for a running host: never exits on its own; records terminate()."""
    def __init__(self):
        self.pid = 4321
        self.terminated = False
    def poll(self):
        return None  # always "still running" so only the watcher can end it
    def terminate(self):
        self.terminated = True


def _fake_clock(step=30.0):
    # A clock that advances by `step` seconds on every call, so a few polls cross any grace.
    state = {"t": 1000.0}
    def now():
        state["t"] += step
        return state["t"]
    return now


def test_watcher_ends_only_the_host_child_after_grace(tmp_path):
    # Zero connections on every poll: the watcher must end the host child once the streak is
    # past the grace, and report recycle=True. It never touches the reader (this process).
    sink = thl.RotatingLog(str(tmp_path / "ts.log"))
    proc = _FakeProc()
    stop = threading.Event()
    # grace=60 with a 30 s/step clock: zero_since set at the first poll, then ~60 s, then >60.
    recycled = thl._watch_zero_connections(
        proc, sink, read_conn=lambda: 0, stop_event=stop,
        grace=60, poll=0, now=_fake_clock(30.0), sleep=lambda s: None)
    sink.close()
    assert recycled is True
    assert proc.terminated is True
    body = (tmp_path / "ts.log").read_text(encoding="utf-8")
    assert "stayed at 0" in body and "start a fresh host" in body


def test_watcher_does_not_recycle_while_connections_return(tmp_path):
    # A single zero poll followed by a healthy one must reset the streak: no recycle, and the
    # watcher stops when asked (stop_event set) rather than looping forever.
    sink = thl.RotatingLog(str(tmp_path / "ts.log"))
    proc = _FakeProc()
    stop = threading.Event()
    counts = iter([0, 5, 0])  # drops, recovers, drops again -- never a sustained streak
    def _read():
        try:
            return next(counts)
        except StopIteration:
            stop.set()  # end the watch after the sequence is exhausted
            return 0
    recycled = thl._watch_zero_connections(
        proc, sink, read_conn=_read, stop_event=stop,
        grace=60, poll=0, now=_fake_clock(30.0), sleep=lambda s: None)
    sink.close()
    assert recycled is False
    assert proc.terminated is False


def test_watcher_waits_when_the_count_is_unreadable(tmp_path):
    # read_conn returning None (CLI hiccup/offline) must NOT be read as zero: no recycle.
    sink = thl.RotatingLog(str(tmp_path / "ts.log"))
    proc = _FakeProc()
    stop = threading.Event()
    calls = {"n": 0}
    def _read():
        calls["n"] += 1
        if calls["n"] >= 4:
            stop.set()
        return None
    recycled = thl._watch_zero_connections(
        proc, sink, read_conn=_read, stop_event=stop,
        grace=60, poll=0, now=_fake_clock(30.0), sleep=lambda s: None)
    sink.close()
    assert recycled is False
    assert proc.terminated is False


def test_serve_forever_restarts_at_once_after_a_zero_conn_recycle(tmp_path, monkeypatch):
    # When run_once reports a recycle, serve_forever must NOT back off and must NOT count it
    # as a failure: a fresh host comes up at once. sleep is patched to record any wait.
    slept = []
    monkeypatch.setattr(thl.time, "sleep", lambda s: slept.append(s))
    outcomes = iter([(0, True), (0, True)])  # two recycles, then stop at max_restarts
    monkeypatch.setattr(
        thl, "run_once",
        lambda cmd, sink, on_exit=None, hook=None, read_conn=None, grace=None, poll=None:
            next(outcomes),
    )
    thl.serve_forever("any-tunnel", root=str(tmp_path), devtunnel="x",
                      max_restarts=2, watch_connections=True)
    log = tmp_path / ".setup" / "logs" / "devtunnel-host-ts.log"
    body = log.read_text(encoding="utf-8")
    assert body.count("recycled at zero connections") == 1  # one restart between two runs
    assert slept == [], "a zero-connection recycle must not back off"


def test_serve_forever_still_backs_off_a_real_crash(tmp_path, monkeypatch):
    # A non-recycle exit (recycled=False) keeps the old fast-fail backoff, unchanged.
    slept = []
    monkeypatch.setattr(thl.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(
        thl, "run_once",
        lambda cmd, sink, on_exit=None, hook=None, read_conn=None, grace=None, poll=None:
            (3, False),
    )
    thl.serve_forever("any-tunnel", root=str(tmp_path), devtunnel="x",
                      max_restarts=3, watch_connections=True)
    assert slept[:2] == [2.0, 4.0]  # unchanged growing backoff for genuine failures
