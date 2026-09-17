# -*- coding: utf-8 -*-
"""A refusal nobody picked up is the harness's problem, not a button on a panel.

WHAT WAS THERE. The settings panel carried a re-unlock control: a worker-name box and a send
button, for the case its own tooltip named -- a worker stopped by a lock that automatic
recovery had not noticed. The operator's objection, twice: that is the harness handing its own
failure to a person, and the person is the part of this system least able to know which
worker, if any, is stuck. The panel gave them a text box to guess into.

WHY THE HOLE WAS REAL. Automatic recovery is driven by the REPLY: _looks_locked runs on the
text that comes back, and a refusal that never produces a recognisable reply is invisible to
it. Measured twice on 2026-09-15 -- a worker refused for lock, no recovery fired, the run
carried on, and once a deliverable came back claiming to have verified content it had never
been able to read.

WHAT REPLACES IT. The server logs every refusal; readers log every classification. A refusal
with no classification after it, past a grace period, was picked up by nobody -- and
relay/turn_windows says which workers had a turn open at that instant. No text box required:
the records already name the candidates.

THE BIAS IS CHOSEN, NOT INHERITED. Unlocking a worker that was not locked costs one turn.
Failing to unlock one that was costs a deliverable that is confidently wrong about work it
never did. So an ambiguous window delivers to every candidate -- the same broadcast the button
offered as an empty target, but selected because the asymmetry says so rather than because
whoever was typing could not tell either.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fleet_runner as FR                 # noqa: E402


class _W:
    def __init__(self, name, status="running"):
        self.name = name
        self.status = status


@pytest.fixture(autouse=True)
def _fresh():
    """The sweep remembers the newest refusal it acted on. Tests must not inherit it."""
    FR._LAST_UNCLAIMED_TS[0] = 0.0
    yield
    FR._LAST_UNCLAIMED_TS[0] = 0.0


def _arrange(monkeypatch, refusals, claims, candidates):
    """Patch the REAL modules' functions, not sys.modules.

    `from tools import lock_state` resolves the attribute on an already-imported package, so
    replacing the sys.modules entry does nothing and the test silently exercises the live log
    instead of its fixture -- which reads as "the sweep delivered nothing" and sends you
    looking at the sweep.
    """
    from tools import lock_state as _ls
    from relay import turn_windows as _tw
    sent = []

    monkeypatch.setattr(_ls, "matching_records", lambda since, now=None: list(refusals))
    monkeypatch.setattr(_ls, "classifications", lambda since, now=None: list(claims))
    monkeypatch.setattr(_tw, "candidates", lambda ts, now=None: list(candidates))

    def _deliver(target, workers, log=None):
        sent.append(target)
        return {"ts": time.time(), "target": target, "ok": True, "delivered": 1, "reason": ""}

    return sent, _deliver


def test_a_refusal_nobody_classified_gets_an_unlock(monkeypatch):
    """The measured hole: refused, nothing fired, the run carried on."""
    now = 1000.0
    sent, deliver = _arrange(monkeypatch, [{"ts": now - 60, "detail": "locked"}], [], ["w1"])
    FR.sweep_unclaimed_refusals([_W("w1")], now=now, log=lambda m: None, deliver=deliver)
    assert sent == ["w1"]


def test_a_refusal_someone_classified_is_left_alone(monkeypatch):
    """The worker's own recovery saw it. Sending a second unlock would spend a turn saying
    something already said."""
    now = 1000.0
    sent, deliver = _arrange(monkeypatch, [{"ts": now - 60, "detail": "locked"}],
                             [{"ts": now - 55, "event": "classified_locked"}], ["w1"])
    FR.sweep_unclaimed_refusals([_W("w1")], now=now, log=lambda m: None, deliver=deliver)
    assert sent == []


def test_a_fresh_refusal_waits_for_the_workers_own_recovery(monkeypatch):
    """Grace, not eagerness: _looks_locked fires when the reply lands, and it should get the
    first attempt. Acting instantly would race the mechanism it exists to back up."""
    now = 1000.0
    sent, deliver = _arrange(monkeypatch, [{"ts": now - 5, "detail": "locked"}], [], ["w1"])
    FR.sweep_unclaimed_refusals([_W("w1")], now=now, log=lambda m: None, deliver=deliver)
    assert sent == []


def test_an_ambiguous_window_delivers_to_every_candidate(monkeypatch):
    """The asymmetry decides. One wasted turn against a deliverable that is confidently wrong
    about work it never did."""
    now = 1000.0
    sent, deliver = _arrange(monkeypatch, [{"ts": now - 60, "detail": "locked"}], [],
                             ["w1", "w2"])
    FR.sweep_unclaimed_refusals([_W("w1"), _W("w2")], now=now, log=lambda m: None,
                                deliver=deliver)
    assert sorted(sent) == ["w1", "w2"]


def test_a_refusal_no_worker_could_own_is_not_broadcast(monkeypatch):
    """Nobody had a turn open: this refusal belongs to something else on the machine. Sending
    to everyone on no evidence is how a recovery becomes the noise it was built to quieten."""
    now = 1000.0
    sent, deliver = _arrange(monkeypatch, [{"ts": now - 60, "detail": "locked"}], [], [])
    FR.sweep_unclaimed_refusals([_W("w1")], now=now, log=lambda m: None, deliver=deliver)
    assert sent == []


def test_a_finished_worker_is_not_woken(monkeypatch):
    now = 1000.0
    sent, deliver = _arrange(monkeypatch, [{"ts": now - 60, "detail": "locked"}], [], ["w1"])
    FR.sweep_unclaimed_refusals([_W("w1", status="done")], now=now, log=lambda m: None,
                                deliver=deliver)
    assert sent == []


def test_the_same_refusal_is_not_delivered_twice(monkeypatch):
    """Sweeps run about once a second. Without a watermark this would re-send for the whole
    freshness window."""
    now = 1000.0
    sent, deliver = _arrange(monkeypatch, [{"ts": now - 60, "detail": "locked"}], [], ["w1"])
    FR.sweep_unclaimed_refusals([_W("w1")], now=now, log=lambda m: None, deliver=deliver)
    FR.sweep_unclaimed_refusals([_W("w1")], now=now + 1, log=lambda m: None, deliver=deliver)
    assert sent == ["w1"]


def test_a_context_less_refusal_is_not_evidence_about_a_worker(monkeypatch):
    """The server's own prefix marks a refusal that belongs to no HTTP turn. _looks_locked
    excludes them for the same reason and this must not disagree with it."""
    from relay.relay_fleet import NO_CONTEXT_REFUSAL
    now = 1000.0
    sent, deliver = _arrange(monkeypatch,
                             [{"ts": now - 60, "detail": NO_CONTEXT_REFUSAL + " x"}], [], ["w1"])
    FR.sweep_unclaimed_refusals([_W("w1")], now=now, log=lambda m: None, deliver=deliver)
    assert sent == []


def test_a_sweep_that_cannot_run_does_not_end_the_run(monkeypatch):
    """A recovery that fell over while recovering would be the failure it exists to prevent."""
    from tools import lock_state as _ls

    def _boom(*_a, **_k):
        raise RuntimeError("log unreadable")

    monkeypatch.setattr(_ls, "matching_records", _boom)
    monkeypatch.setattr(_ls, "classifications", _boom)
    said = []
    out = FR.sweep_unclaimed_refusals([_W("w1")], now=1000.0, log=said.append)
    assert out == []
    assert any("skipped" in m for m in said), "a swallowed failure must still say so"


def test_the_panel_no_longer_offers_the_control():
    """THE INSTRUCTION, pinned. A person should not be asked which worker is stuck; the
    records already know."""
    import io
    body = io.open(os.path.join(REPO, "ui", "FleetCockpit.cs"),
                   encoding="utf-8-sig", errors="replace").read()
    assert "void RequestReunlock" not in body
    assert "_reunlockBtn" not in body
    assert "set_reunlock_section" not in body or "SectionHeader(T(\"set_reunlock_section\"))" \
        not in body, "the re-unlock section is still built into the settings panel"


def test_the_sweep_is_actually_called_every_tick():
    """A recovery nothing calls is the button's failure mode with better manners."""
    import io
    src = io.open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8").read()
    i = src.index("def on_tick(")
    j = src.index("\n    from playwright", i)
    assert "sweep_unclaimed_refusals(" in src[i:j], "on_tick does not run the sweep"
