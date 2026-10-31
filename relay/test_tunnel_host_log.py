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
import time

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
    rc = thl.run_once([sys.executable, "-c", prog], sink)
    elapsed = time.time() - start
    sink.close()
    assert rc == 0
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
        lambda cmd, sink, on_exit=None, hook=None: real_run_once(fake, sink, hook=hook),
    )
    thl.serve_forever("any-tunnel", root=str(tmp_path), devtunnel=sys.executable,
                      max_bytes=10 * 1024 * 1024, backups=2, max_restarts=3)
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

    def _boom(cmd, sink, on_exit=None, hook=None):
        raise FileNotFoundError("devtunnel not found")

    monkeypatch.setattr(thl, "run_once", _boom)
    thl.serve_forever("any-tunnel", root=str(tmp_path), devtunnel="nope",
                      max_restarts=1)
    log = tmp_path / ".setup" / "logs" / "devtunnel-host-ts.log"
    body = log.read_text(encoding="utf-8")
    assert "cannot start host" in body
    assert "host exited code=127" in body
