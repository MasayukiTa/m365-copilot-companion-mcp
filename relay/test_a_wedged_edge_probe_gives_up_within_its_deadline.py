# -*- coding: utf-8 -*-
"""A recovery health probe against a wedged Edge must give up on a deadline, not hang forever.

THE BUG THIS PINS. relay/edge_recover.cdp_alive() passes its timeout only to
connect_over_cdp -- that bounds the CONNECT. The very next line, ``_ = b.contexts``, is a
synchronous CDP round-trip with NO timeout. A browser that is wedged-but-still-listening
(TCP accepts, /json/version answers in ~1.7s, but the CDP command never returns) blocks that
line forever. The fleet recovery loop (relay/fleet_runner.py) called cdp_alive() on its MAIN
thread, so that one line froze the whole coordinator: status.json stopped advancing and the
stall was silent. Observed 2026-11-07..09: recovery attempt 1 stood still for ~33 h inside
exactly this call while the DevTools HTTP endpoint still answered.

TWO GUARANTEES ARE PINNED HERE:

  1. cdp_alive_bounded() wraps the probe in a hard wall-clock ceiling: when the underlying
     probe hangs, it returns False WITHIN the deadline (and does not block the caller past
     it), which routes recovery straight to a hard reset -- the correct action for a wedged
     browser.

  2. _stamp_recovery_status() keeps status.json ADVANCING during recovery: it merges a fresh
     `updated` and a `recovery` block into the existing snapshot (preserving running /
     workers / the rest), so a stuck recovery is VISIBLE to a watcher and gets retried
     instead of sitting silent.

No real browser and no Windows-only calls: the hang is simulated with a monkeypatched
cdp_alive that sleeps, so this runs on the Linux CI.
"""
from __future__ import annotations

import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.edge_recover as er                      # noqa: E402


def test_bounded_probe_gives_up_within_deadline_when_connect_hangs(monkeypatch):
    """A hung cdp_alive must be abandoned: bounded probe returns False at/under the deadline."""
    started = []

    def _hang(cdp_url="http://localhost:9222", timeout_ms=5000):
        # Simulate the wedged browser: the probe never returns in any reasonable time.
        started.append(time.time())
        time.sleep(60.0)
        return True  # would "succeed" eventually, but far past any deadline

    monkeypatch.setattr(er, "cdp_alive", _hang)

    deadline_s = 0.5
    t0 = time.time()
    alive = er.cdp_alive_bounded("http://127.0.0.1:9222", deadline_s=deadline_s)
    elapsed = time.time() - t0

    assert started, "the probe was never actually invoked"
    # It gave up: a wedged browser reads as NOT alive, so the caller will hard-reset it.
    assert alive is False
    # And it gave up ON the deadline, not after the 60s sleep: the whole point is bounded.
    assert elapsed < deadline_s + 2.0, (
        "bounded probe waited %.1fs, well past its %.1fs deadline" % (elapsed, deadline_s))


def test_bounded_probe_returns_true_when_underlying_probe_says_alive(monkeypatch):
    """The wrapper must not change the answer for a healthy browser -- only bound a hung one."""
    monkeypatch.setattr(er, "cdp_alive",
                        lambda cdp_url="", timeout_ms=5000: True)
    assert er.cdp_alive_bounded("http://127.0.0.1:9222", deadline_s=5.0) is True

    monkeypatch.setattr(er, "cdp_alive",
                        lambda cdp_url="", timeout_ms=5000: False)
    assert er.cdp_alive_bounded("http://127.0.0.1:9222", deadline_s=5.0) is False


def _load_fleet_runner():
    import importlib
    import relay.fleet_runner as fr
    importlib.reload(fr)
    return fr


def test_recovery_stamp_advances_status_json_during_recovery(tmp_path):
    """_stamp_recovery_status must MERGE a fresh `updated` + recovery block, not freeze/clobber."""
    fr = _load_fleet_runner()
    status_path = str(tmp_path / "status.json")

    # A realistic live snapshot the cockpit would be reading.
    with open(status_path, "w", encoding="utf-8") as fh:
        json.dump({"started": 1000.0, "updated": 1000.0,
                   "running": True, "workers": [{"name": "w0"}]}, fh)
    before = os.path.getmtime(status_path)
    time.sleep(0.05)

    fr._stamp_recovery_status(status_path, attempt=1, phase="backoff:Edge context lost",
                              pending_count=17)

    with open(status_path, encoding="utf-8-sig") as fh:
        snap = json.load(fh)

    # Preserved the fields the cockpit reads (merge, not replace).
    assert snap.get("running") is True
    assert snap.get("workers") == [{"name": "w0"}]
    # Advanced: updated moved forward and a recovery block appeared.
    assert snap.get("updated", 0) > 1000.0
    assert snap.get("recovering") is True
    rec = snap.get("recovery") or {}
    assert rec.get("attempt") == 1
    assert rec.get("pending") == 17
    assert "backoff" in str(rec.get("phase", ""))
    # The file itself was rewritten (mtime advanced) -> a watcher sees progress.
    assert os.path.getmtime(status_path) >= before


def test_recovery_stamp_is_best_effort_on_unreadable_status(tmp_path):
    """A missing/unreadable status.json must not make the stamp raise -- recovery must go on."""
    fr = _load_fleet_runner()
    missing = str(tmp_path / "does_not_exist.json")
    # Must not raise even though there is nothing to merge.
    fr._stamp_recovery_status(missing, attempt=2, phase="edge-unreachable-hard-reset",
                              pending_count=3)
    # It writes a fresh snapshot rather than crashing.
    assert os.path.exists(missing)
    with open(missing, encoding="utf-8-sig") as fh:
        snap = json.load(fh)
    assert snap.get("recovering") is True
    assert (snap.get("recovery") or {}).get("attempt") == 2
