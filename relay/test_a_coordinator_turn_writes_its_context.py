# -*- coding: utf-8 -*-
"""A fleet turn leaves an open row at the send and a close row at the reply, in
.fleet/turn_context.jsonl, so the MCP server can attribute that turn's tool calls."""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.relay_fleet as rf  # noqa: E402
from tools import turn_context as TC  # noqa: E402


class _Answers:
    def count(self):
        return 0


class _SendDrv:
    def __init__(self):
        self._count_before = 0

    def _answers(self):
        return _Answers()

    def send(self, text, **kw):
        pass


def _rows(path):
    return [json.loads(x) for x in open(path, encoding="utf-8").read().splitlines() if x.strip()]


def test_a_turn_writes_an_open_row_then_a_close_row(tmp_path, monkeypatch):
    monkeypatch.setattr(TC, "CONTEXT_PATH", str(tmp_path / "turn_context.jsonl"), raising=False)
    w = rf.RelayWorker({"text": "do work", "jid": "J-42"}, "w0", refuter=False, max_research=0,
                       run_id="runA")
    w.socket = True
    w.page = None
    w.drv = _SendDrv()
    w.status = "ready"
    w.job = "do work"
    monkeypatch.setattr(rf, "admission_is_due", lambda now=None: True)
    monkeypatch.setattr(rf, "note_admitted", lambda now=None: None)

    w._begin_send()
    opened = _rows(TC.CONTEXT_PATH)
    assert len(opened) == 1 and opened[0]["event"] == "open"
    assert (opened[0]["worker"], opened[0]["job"], opened[0]["run"], opened[0]["turn"]) \
        == ("w0", "J-42", "runA", 1)
    assert opened[0]["t_send"] > 0 and opened[0]["pid"] == os.getpid()

    w._decide("まだ作業中です。")
    both = _rows(TC.CONTEXT_PATH)
    assert [r["event"] for r in both] == ["open", "close"]
    assert both[1]["t_send"] == opened[0]["t_send"] and both[1]["t_done"] >= both[1]["t_send"]
    # and the server-side reader sees exactly that window
    assert TC.candidates((both[1]["t_send"] + both[1]["t_done"]) / 2) == [("w0", "J-42")]
