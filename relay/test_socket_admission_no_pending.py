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
    def needs_refresh(self):
        return False
    def record(self, *a, **k):
        pass


class _NeverReopen:
    def consider(self, route):
        return False


def test_socket_batch_leaves_no_worker_pending_even_when_tab_cap_is_two(monkeypatch):
    """The measured defect: open_tabs=0, mc_box=2, yet workers 5/6 stayed PENDING."""
    rf._reset_admission_pacing()
    monkeypatch.setattr(rf, "_socket_route", lambda: _Route())
    monkeypatch.setattr(rf, "_reopen_policy", lambda: _NeverReopen())
    monkeypatch.setattr(rf, "free_disk_gb", lambda path=None: 500.0)
    monkeypatch.setattr(rf, "avail_phys_mb", lambda: 64000.0)

    attached = []
    first = {}
    stop_box = [False]

    def attach(self, context, agent_url):
        self.page = None
        self.socket = True
        self.drv = object()
        self.status = "ready"
        attached.append(self.name)
        return True

    def poll(self):
        return False

    def close(self):
        self.closed = True
        self.page = None
        self.drv = None

    monkeypatch.setattr(rf.RelayWorker, "attach", attach)
    monkeypatch.setattr(rf.RelayWorker, "poll", poll)
    monkeypatch.setattr(rf.RelayWorker, "close", close)

    def tick(workers):
        if first:
            return
        first["pending"] = [w.name for w in workers if w.status == rf.PENDING]
        first["ready"] = [w.name for w in workers if w.status == "ready"]
        first["tab_load"] = sum(w.tab_load() for w in workers)
        stop_box[0] = True

    rf.run_relay_fleet(_Ctx(), ["g%d" % i for i in range(6)], "http://agent",
                       max_concurrent=2, poll_s=0, on_tick=tick,
                       notify=lambda *a, **k: None, stop_box=stop_box,
                       autoscale=False, disk_floor_gb=0.0)

    assert first["tab_load"] == 0
    assert first["pending"] == [], first
    assert len(first["ready"]) == 6
    assert len(attached) == 6


def test_tab_batch_still_obeys_the_same_tab_cap(monkeypatch):
    """Removing the socket worker-count gate must not remove the real browser-tab budget."""
    rf._reset_admission_pacing()
    monkeypatch.setattr(rf, "_socket_route", lambda: type("R", (), {"open": lambda self: False})())
    monkeypatch.setattr(rf, "admission_is_due", lambda now=None: True)
    monkeypatch.setattr(rf, "free_disk_gb", lambda path=None: 500.0)
    monkeypatch.setattr(rf, "avail_phys_mb", lambda: 64000.0)

    first = {}
    stop_box = [False]

    def attach(self, context, agent_url):
        self.page = object()
        self.socket = False
        self.drv = object()
        self.status = "ready"
        return True

    monkeypatch.setattr(rf.RelayWorker, "attach", attach)
    monkeypatch.setattr(rf.RelayWorker, "poll", lambda self: False)
    monkeypatch.setattr(rf.RelayWorker, "close", lambda self: setattr(self, "closed", True))

    def tick(workers):
        if first:
            return
        first["pending"] = sum(w.status == rf.PENDING for w in workers)
        first["open"] = sum(w.page is not None for w in workers)
        stop_box[0] = True

    rf.run_relay_fleet(_Ctx(), ["g%d" % i for i in range(6)], "http://agent",
                       max_concurrent=2, poll_s=0, on_tick=tick,
                       notify=lambda *a, **k: None, stop_box=stop_box,
                       autoscale=False, disk_floor_gb=0.0)
    assert first == {"pending": 4, "open": 2}


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

    monkeypatch.setattr(rf, "admission_is_due", lambda now=None: False)
    w._begin_send()
    assert w.status == "ready"
    assert w.drv.sent == []

    sent_meter = []
    monkeypatch.setattr(rf, "admission_is_due", lambda now=None: True)
    monkeypatch.setattr(rf, "note_admitted", lambda now=None: sent_meter.append(time.time()))
    w._begin_send()
    assert w.drv.sent == ["do work"]
    assert w.status == "waiting"
    assert len(sent_meter) == 1
