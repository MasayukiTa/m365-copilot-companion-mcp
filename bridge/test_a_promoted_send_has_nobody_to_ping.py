# -*- coding: utf-8 -*-
"""A promoted /send has no SSE client, and the turn loop was poking one anyway.

THE REPORT. /send answered {"ok":true,"queued":true,"promotion_attempted":true,"note":"queued,
and a turn is being run for it now."} and no turn was ever recorded. store.turns stayed at
1182, newest_ts stayed on 09-10, the session created for it had turns:0 and an empty conv_url,
and `pending` was EMPTY -- so the message had been taken out of the queue and had gone nowhere.
One short line behaved exactly like a 13.7 KB one, so size was not it. The bridge was idle,
transport socket, has_resident_page false.

THE CAUSE, from .setup/logs/bridge.log:

    09:59:32 WARNING drain_pending_queue: queued send failed for sid='s0918005930cea3'
      File "bridge/copilot_bridge.py", line 5279, in _drain_pending_queue
        final = self._run_one_turn(sid, item, stream_out=False)
      ...
      File "bridge/copilot_bridge.py", line 4795, in _send_and_stream_once
        self._ping()                     # detect Esc/Stop disconnect promptly
      OSError: [WinError 10038] an operation was attempted on something that is not a socket

_ping writes an SSE comment whose purpose is to RAISE when the client has hung up. Every
self._sse(...) in that loop is guarded by `if stream_out`; the ping was not -- and it is the
one write whose whole job is to fail. On the promoted path there is no client at all: /send
writes its JSON reply and returns, and the turn starts afterwards on another thread, so
self.wfile is the finished socket of a request nobody is reading. Every promoted send died
0.3 s into the loop, before an answer could be read.

THE SECOND DEFECT, WHICH IS WHY IT WAS SILENT AND LOSSY. drain_pending_once pops every item up
front, so anything not delivered has to be put back explicitly. The wrong-session branch does
exactly that, and says in its own comment that the first version lost messages and a test
caught it. The two branches beside it -- the turn raised, and the turn returned nothing -- did
not. So the operator's instruction was consumed by a `logger.warning` and an `except` block.
"""
from __future__ import annotations

import io
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = io.open(os.path.join(REPO, "bridge", "copilot_bridge.py"),
              encoding="utf-8", errors="replace").read()


def _body(name):
    """The source of one method, up to the next def at the same indent."""
    m = re.search(r"\n    def %s\(.*?(?=\n    def )" % re.escape(name), SRC, re.S)
    assert m, "method %s not found" % name
    return m.group(0)


def test_the_turn_loop_never_pings_without_a_stream():
    """THE CAUSE. Asserted on the source because the alternative is standing up an HTTP
    handler with a dead socket, and what went wrong is one unguarded statement."""
    body = _body("_send_and_stream_once")
    assert "self._ping()" in body, "the ping is gone entirely; a real stream needs it"
    for line_no, line in enumerate(body.splitlines()):
        if line.strip().startswith("self._ping()"):
            before = body.splitlines()[max(0, line_no - 3):line_no]
            assert any("if stream_out" in b for b in before), \
                "an unguarded self._ping() is back at offset %d" % line_no


def test_the_streaming_paths_keep_their_ping():
    """The guard must not be applied where the ping is the point. _review_stream and
    _run_fix_subprocess only ever run with a live SSE consumer."""
    for name in ("_review_stream", "_run_fix_subprocess"):
        assert "self._ping()" in _body(name), "%s lost its disconnect detection" % name



def test_review_subprocesses_do_not_allocate_console_windows():
    """Bridge review helpers are unattended children of a background service."""
    for name in ("_review_stream", "_run_fix_subprocess"):
        body = _body(name)
        assert "subprocess.Popen" in body
        assert "creationflags=childproc.headless_creationflags()" in body, (
            "%s can allocate a visible console window" % name)


def test_every_drain_branch_either_delivers_or_puts_it_back():
    """THE SECOND DEFECT. Each way out of the per-item block must end in a delivery, a
    re-queue, or a written record -- never in a bare log line."""
    body = _body("_drain_pending_queue")
    assert "_persist_exchange" in body
    assert "_queue_input_locked" in body, "the wrong-session re-queue is gone"
    assert body.count("_retry_or_record") >= 3, \
        "a failure branch still drops the message: %d call(s)" % body.count("_retry_or_record")


# ---- the retry itself, driven rather than read -------------------------------------------

@pytest.fixture()
def bridge(monkeypatch, tmp_path):
    import bridge.copilot_bridge as B

    monkeypatch.setattr(B, "UNDELIVERED_PATH", str(tmp_path / "undelivered.jsonl"))
    B._DRAIN_ATTEMPTS.clear()
    queued = []
    monkeypatch.setattr(B, "_queue_input_locked", lambda sid, text: queued.append((sid, text)))
    return B, queued, tmp_path


def test_a_first_failure_puts_the_message_back(bridge):
    B, queued, _ = bridge
    assert B._retry_or_record("s1", "書き直して", "boom") is True
    assert queued == [("s1", "書き直して")]


def test_a_message_that_keeps_failing_is_recorded_not_retried_forever(bridge):
    """It must not sit in front of the next instruction. What it must not do is vanish."""
    import json

    B, queued, tmp = bridge
    B._retry_or_record("s1", "書き直して", "boom")
    assert B._retry_or_record("s1", "書き直して", "boom again") is False
    assert len(queued) == 1, "it was re-queued a second time"
    rows = [json.loads(l) for l in io.open(B.UNDELIVERED_PATH, encoding="utf-8") if l.strip()]
    assert rows[0]["sid"] == "s1"
    assert rows[0]["text"] == "書き直して"
    assert "boom again" in rows[0]["why"]


def test_two_different_messages_do_not_share_a_budget(bridge):
    B, queued, _ = bridge
    assert B._retry_or_record("s1", "one", "boom") is True
    assert B._retry_or_record("s1", "two", "boom") is True
    assert len(queued) == 2


def test_recording_a_loss_cannot_raise_on_the_failure_path(bridge, monkeypatch, tmp_path):
    """This runs while something has already gone wrong. It must not add a second failure.

    THE FIRST VERSION PASSED FOR THE WRONG REASON AND LEFT LITTER. It handed
    os.path.join("no", "such", "dir", "x.jsonl") meaning "a path that cannot be written" --
    but _record_undelivered calls os.makedirs(exist_ok=True), so the path was created, the
    write SUCCEEDED, and the assertion that nothing raised was about a happy path. It also
    created no/such/dir/ in the repository root, which is how it was noticed.

    A directory where a file must go is genuinely unwritable on every platform this runs on,
    and it is inside tmp_path so nothing is left behind either way."""
    B, _, _ = bridge
    blocked = tmp_path / "undeliverable"
    blocked.mkdir()                      # a DIRECTORY at the path the writer wants for a file
    monkeypatch.setattr(B, "UNDELIVERED_PATH", str(blocked))
    B._DRAIN_ATTEMPTS[("s1", "x")] = 99
    assert B._retry_or_record("s1", "x", "boom") is False      # must not raise
    assert blocked.is_dir(), "the writer replaced the directory instead of failing"
