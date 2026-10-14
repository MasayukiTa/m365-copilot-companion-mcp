# -*- coding: utf-8 -*-
"""Socket workers are not browser tabs: admission must never queue them behind a tab/RAM cap.

Rate/quota protection belongs at the actual generative send.  This file pins both halves of
that contract so a future tab-memory fix cannot silently turn back into a socket worker cap.
"""
from __future__ import annotations

import time

import pytest

from relay import relay_fleet as rf


class _Ctx:
    def cookies(self):
        return []


class _Route:
    closed_reason = ""
    def open(self):
        return True
    def ready(self, agent_url=None):
        return True
    def needs_refresh(self, agent_url=None):
        return False
    def record(self, *a, **k):
        pass


class _NeverReopen:
    def consider(self, route):
        return False


def _run(monkeypatch, route, max_concurrent, n=6, attach_fn=None):
    rf._reset_admission_pacing()
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    monkeypatch.setattr(rf, "_reopen_policy", lambda: _NeverReopen())
    monkeypatch.setattr(rf, "free_disk_gb", lambda path=None: 500.0)
    monkeypatch.setattr(rf, "avail_phys_mb", lambda: 64000.0)

    first = {}
    stop_box = [False]

    def attach(self, context, agent_url):
        self.page = None
        self.socket = True
        self.drv = object()
        self.status = "ready"
        return True

    monkeypatch.setattr(rf.RelayWorker, "attach", attach)
    monkeypatch.setattr(rf.RelayWorker, "poll", lambda self: False)
    monkeypatch.setattr(rf.RelayWorker, "close", lambda self: setattr(self, "closed", True))

    def tick(workers):
        if first:
            return
        first["pending"] = [w.name for w in workers if w.status == rf.PENDING]
        first["ready"] = [w.name for w in workers if w.status == "ready"]
        first["tab_load"] = sum(w.tab_load() for w in workers)
        first["reasons"] = [w.reason for w in workers if w.status == rf.PENDING]
        stop_box[0] = True

    rf.run_relay_fleet(_Ctx(), ["g%d" % i for i in range(n)], "http://agent",
                       max_concurrent=max_concurrent, poll_s=0, on_tick=tick,
                       notify=lambda *a, **k: None, stop_box=stop_box,
                       autoscale=False, disk_floor_gb=0.0)
    return first


def test_socket_batch_is_admitted_up_to_the_lane_ceiling_and_holds_no_tab(monkeypatch):
    """The lane ceiling (not a tab budget) bounds concurrent socket goals; nothing holds a tab."""
    first = _run(monkeypatch, _Route(), max_concurrent=2)
    assert first["tab_load"] == 0
    assert len(first["ready"]) == 2 and len(first["pending"]) == 4, first


def test_a_high_ceiling_admits_the_whole_batch_in_one_sweep(monkeypatch):
    first = _run(monkeypatch, _Route(), max_concurrent=200)
    assert first["pending"] == [] and len(first["ready"]) == 6 and first["tab_load"] == 0


def test_without_a_token_every_goal_waits_with_a_reason_and_no_tab_opens(monkeypatch):
    class _NoToken(_Route):
        enabled = True

        def ready(self, agent_url=None):
            return False

        def driver_for(self, *a, **k):
            return None

    first = _run(monkeypatch, _NoToken(), max_concurrent=200)
    assert len(first["pending"]) == 6 and first["ready"] == [] and first["tab_load"] == 0
    assert all(r.startswith("waiting for token") for r in first["reasons"]), first


class _Answers:
    def count(self):
        return 0


class _SendDrv:
    def __init__(self):
        self.sent = []
        self._count_before = 0
    def _answers(self):
        return _Answers()
    def send(self, text, **kw):
        self.sent.append(text)


def test_socket_rate_pacing_happens_at_send_not_admission(monkeypatch):
    w = rf.RelayWorker("do work", "w0", refuter=False, max_research=0)
    w.socket = True
    w.page = None
    w.drv = _SendDrv()
    w.status = "ready"
    w.job = "do work"

    monkeypatch.setattr(rf, "socket_send_due", lambda now=None: False)
    w._begin_send()
    assert w.status == "ready"
    assert w.drv.sent == []

    sent_meter = []
    monkeypatch.setattr(rf, "socket_send_due", lambda now=None: True)
    monkeypatch.setattr(rf, "note_socket_sent", lambda now=None: sent_meter.append(time.time()))
    w._begin_send()
    assert w.drv.sent == ["do work"]
    assert w.status == "waiting"
    assert len(sent_meter) == 1
