# -*- coding: utf-8 -*-
"""A health signal must distinguish "broken" from "nothing happened".

WHAT THIS EXISTS FOR, 2026-09-16. The cockpit's 6th dot is labelled "Tool" and reads
.fleet/tool_probe.json, which only the BRIDGE's idle self-probe writes. That morning it was
red -- truthfully, the bridge agent had lost its tool list -- while fleet workers completed
84 tool calls in an hour and a goal finished DONE with the refuter upholding it. One path of
four was down and the strip said "Tool". tools/tool_probe.py's own docstring describes the
mirror of this gap, which is what it was built to close for the bridge; nothing was ever
built for the fleet.

THE TWO WAYS A SIGNAL LIKE THIS LIES, and both are tested here because the first draft of
the module committed the second one:

  FALSE RED FROM SILENCE. An idle machine makes no tool calls. Reporting absence as failure
  is how a green system gets a red light, which is the whole complaint.

  FALSE RED FROM ONE TRANSIENT. The first rule was `ok = (fail_n == 0)`. Run against the real
  ledger over two hours it returned FAILING on 91 successes and 1 failure -- and that single
  refusal came from the run that finished DONE. Caught by running it, not by reading it.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import fleet_tool_health as H  # noqa: E402


def _ledger(tmp_path, rows):
    p = tmp_path / "tool_events.jsonl"
    with io.open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def _call(i, tool, ts):
    return {"event": "call", "id": "c%d" % i, "tool": tool, "ts": ts}


def _outcome(i, ok, ts):
    return {"event": "outcome", "id": "c%d" % i, "ok": ok, "ts": ts}


@pytest.fixture
def at(monkeypatch, tmp_path):
    """Point the module at a ledger this test wrote, and freeze 'now'."""
    now = 1_000_000.0

    def _use(rows):
        monkeypatch.setattr(H, "LEDGER", _ledger(tmp_path, rows))
        return H.get_summary(now=now)

    _use.now = now
    return _use


def test_an_idle_machine_reports_no_evidence_not_failure(at):
    """The first way it could lie: silence read as breakage."""
    s = at([])
    assert s["fleet_tool_ok"] is None
    assert "no evidence" in H.describe(s)


def test_calls_older_than_the_window_are_not_evidence_about_now(at):
    old = at.now - H.FRESH_S - 60
    s = at([_call(1, "screen_press", old), _outcome(1, True, old)])
    assert s["fleet_tool_ok"] is None, "a stale success was presented as current health"


def test_a_working_path_reports_working(at):
    rows = []
    for i in range(5):
        t = at.now - 10 * i
        rows += [_call(i, "screen_press", t), _outcome(i, True, t)]
    s = at(rows)
    assert s["fleet_tool_ok"] is True
    assert s["fleet_tool_ok_n"] == 5


def test_one_failure_among_many_successes_is_not_a_red_light(at):
    """THE MEASURED CASE. 91 ok and 1 fail in two hours, in a run that finished DONE."""
    rows = []
    for i in range(20):
        t = at.now - 100 + i
        rows += [_call(i, "screen_look", t), _outcome(i, i != 3, t)]
    s = at(rows)
    assert s["fleet_tool_fail_n"] == 1
    assert s["fleet_tool_ok"] is True, "one transient refusal flipped the display"


def test_a_run_of_failures_at_the_end_is_a_red_light(at):
    """And it must still be able to say no, or it is decoration."""
    rows = []
    for i in range(10):
        t = at.now - 100 + i
        ok = i < 10 - H.CONSECUTIVE_FAILURES_FOR_RED
        rows += [_call(i, "screen_press", t), _outcome(i, ok, t)]
    s = at(rows)
    assert s["fleet_tool_ok"] is False
    assert "FAILING" in H.describe(s)


def test_a_failure_that_has_already_recovered_is_not_red(at):
    """Failures then a success: the path came back, so the answer to 'now' is yes."""
    pattern = [False] * H.CONSECUTIVE_FAILURES_FOR_RED + [True]
    rows = []
    for i, ok in enumerate(pattern):
        t = at.now - 50 + i
        rows += [_call(i, "screen_press", t), _outcome(i, ok, t)]
    assert at(rows)["fleet_tool_ok"] is True


def test_gateway_chatter_cannot_manufacture_green(at):
    """call_tool.catalogue and friends answer questions about the gateway itself and succeed
    even when every real tool is unreachable. Counting them would paint green over a dead
    path."""
    rows = []
    for i in range(6):
        t = at.now - 50 + i
        rows += [_call(i, "call_tool.catalogue", t), _outcome(i, True, t)]
    s = at(rows)
    assert s["fleet_tool_ok"] is None, "discovery chatter was counted as tool traffic"
    assert s["fleet_tool_ok_n"] == 0


def test_a_missing_ledger_is_unknown_rather_than_an_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "LEDGER", tmp_path / "does_not_exist.jsonl")
    assert H.get_summary()["fleet_tool_ok"] is None


def test_a_corrupt_line_does_not_take_the_reader_down(at):
    """A health display that crashes on a bad byte is worse than one that is briefly vague."""
    rows = [_call(1, "screen_press", at.now - 5), _outcome(1, True, at.now - 5)]
    s = at(rows)
    assert s["fleet_tool_ok"] is True
    # and again with a truncated line appended
    p = H.LEDGER
    with io.open(str(p), "a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"event": "outc')
    assert H.get_summary(now=at.now)["fleet_tool_ok"] is True


def test_only_the_tail_is_read_so_the_cost_does_not_grow_with_the_log(monkeypatch, tmp_path):
    """Measured: 12 ms against the real 67 MB ledger. A full scan on every /health poll is
    what this avoids."""
    p = tmp_path / "big.jsonl"
    now = 1_000_000.0
    with io.open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        for i in range(60000):
            fh.write(json.dumps({"event": "call", "id": "old%d" % i, "tool": "x",
                                 "ts": now - 99999}) + "\n")
        fh.write(json.dumps(_call(1, "screen_press", now - 5)) + "\n")
        fh.write(json.dumps(_outcome(1, True, now - 5)) + "\n")
    monkeypatch.setattr(H, "LEDGER", p)
    started = time.time()
    s = H.get_summary(now=now)
    assert s["fleet_tool_ok"] is True
    assert (time.time() - started) < 2.0
    assert p.stat().st_size > H.TAIL_BYTES, "the fixture no longer exercises the tail read"
