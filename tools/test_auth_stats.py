"""Hermetic tests for tools/auth_stats.py -- the auth-failure sliding-window
tracker added after the incident where Copilot Studio's stored API key
desynced from MCP_API_KEY and every /mcp call 401'd with zero surfaced
signal (see auth_stats.py module docstring).

Covers: pure AuthFailureTracker logic (recording, window pruning, summary
shape) using an isolated instance -- no shared/module state, no I/O, no
network, no real request. Also covers the module-level record_auth_failure /
get_summary / write_snapshot wrappers, redirecting _STATS_FILE to a tmp_path
so no test ever touches the real .fleet/auth_stats.json.

Run: pytest -q tools\\test_auth_stats.py
"""
from __future__ import annotations

import json

from tools import auth_stats


# ===========================================================================
# 1. AuthFailureTracker: pure logic, isolated instances
# ===========================================================================


def test_empty_tracker_summary_is_zeroed():
    t = auth_stats.AuthFailureTracker(window_s=600.0)
    s = t.summary(ts=1_000_000.0)
    assert s == {"auth_fail_10m": 0, "auth_fail_last_ts": None}


def test_record_increments_count_and_last_ts():
    t = auth_stats.AuthFailureTracker(window_s=600.0)
    t.record(ts=1000.0)
    t.record(ts=1001.0)
    t.record(ts=1002.5)
    s = t.summary(ts=1003.0)
    assert s["auth_fail_10m"] == 3
    assert s["auth_fail_last_ts"] == 1002.5


def test_window_prunes_events_older_than_window():
    t = auth_stats.AuthFailureTracker(window_s=600.0)
    t.record(ts=0.0)          # will age out
    t.record(ts=100.0)        # still within 600s of ts=650 (cutoff is 50)
    t.record(ts=500.0)        # still within 600s of ts=650
    now = 650.0
    s = t.summary(ts=now)
    # cutoff = 650 - 600 = 50 -> ts=0.0 ages out, ts=100.0 and ts=500.0 survive
    assert s["auth_fail_10m"] == 2
    assert s["auth_fail_last_ts"] == 500.0

    # Push further out so even ts=100.0 ages out too.
    s2 = t.summary(ts=1000.0)  # cutoff = 400 -> only ts=500.0 survives
    assert s2["auth_fail_10m"] == 1
    # last_ts is NOT windowed -- it reports the most recent rejection ever,
    # even once earlier events have aged out of the 10-minute count.
    assert s2["auth_fail_last_ts"] == 500.0


def test_last_ts_persists_after_window_empties_entirely():
    t = auth_stats.AuthFailureTracker(window_s=10.0)
    t.record(ts=0.0)
    s = t.summary(ts=1000.0)  # far past the window
    assert s["auth_fail_10m"] == 0
    assert s["auth_fail_last_ts"] == 0.0, "last_ts must survive even once the window is empty"


def test_record_is_monotonic_ok_with_out_of_order_ts_within_window():
    """record() should not crash or misbehave if timestamps arrive slightly
    out of order (e.g. clock skew across threads); it only needs to keep the
    window-prune logic correct as of the LATEST summary() call time."""
    t = auth_stats.AuthFailureTracker(window_s=600.0)
    t.record(ts=100.0)
    t.record(ts=90.0)  # slightly earlier than the previous event
    t.record(ts=110.0)
    s = t.summary(ts=120.0)
    assert s["auth_fail_10m"] == 3


def test_default_ts_uses_wallclock(monkeypatch):
    """When record()/summary() are called with no explicit ts, they must fall
    back to time.time() (exercised via the real function, not frozen) -- this
    just proves no ts is REQUIRED and nothing raises."""
    t = auth_stats.AuthFailureTracker(window_s=600.0)
    t.record()
    s = t.summary()
    assert s["auth_fail_10m"] >= 1
    assert isinstance(s["auth_fail_last_ts"], float)


def test_independent_instances_do_not_share_state():
    """Two AuthFailureTracker instances must never leak events into each
    other -- guards against accidentally storing state at class/module level
    instead of per-instance."""
    a = auth_stats.AuthFailureTracker(window_s=600.0)
    b = auth_stats.AuthFailureTracker(window_s=600.0)
    a.record(ts=1.0)
    a.record(ts=2.0)
    assert a.summary(ts=3.0)["auth_fail_10m"] == 2
    assert b.summary(ts=3.0)["auth_fail_10m"] == 0


# ===========================================================================
# 2. Module-level wrappers: record_auth_failure / get_summary / write_snapshot
# ===========================================================================


def test_record_auth_failure_updates_module_singleton_and_writes_snapshot(monkeypatch, tmp_path):
    fresh = auth_stats.AuthFailureTracker(window_s=600.0)
    monkeypatch.setattr(auth_stats, "_TRACKER", fresh)
    stats_file = tmp_path / "auth_stats.json"
    monkeypatch.setattr(auth_stats, "_STATS_FILE", stats_file)

    # Use a real, "now"-ish timestamp (not a stale epoch value) so it survives
    # the wallclock-anchored pruning that get_summary()/write_snapshot() do
    # internally (they call summary() with no ts override).
    import time as _time
    now = _time.time()
    auth_stats.record_auth_failure(ts=now)

    summary = auth_stats.get_summary()
    assert summary["auth_fail_10m"] == 1
    assert summary["auth_fail_last_ts"] == now

    assert stats_file.is_file(), "write_snapshot must have created .fleet/auth_stats.json"
    on_disk = json.loads(stats_file.read_text(encoding="utf-8"))
    assert on_disk == summary


def test_write_snapshot_is_atomic_no_tmp_left_behind(monkeypatch, tmp_path):
    fresh = auth_stats.AuthFailureTracker(window_s=600.0)
    monkeypatch.setattr(auth_stats, "_TRACKER", fresh)
    stats_file = tmp_path / "nested" / "auth_stats.json"
    monkeypatch.setattr(auth_stats, "_STATS_FILE", stats_file)

    fresh.record(ts=1.0)
    auth_stats.write_snapshot()

    assert stats_file.is_file()
    tmp_sibling = stats_file.parent / (stats_file.name + ".tmp")
    assert not tmp_sibling.exists(), "temp file must be replaced away, not left behind"


def test_write_snapshot_never_raises_on_unwritable_path(monkeypatch):
    """Point _STATS_FILE somewhere that cannot possibly be created (a path
    with a NUL byte is invalid on every OS) and confirm write_snapshot()
    swallows the failure instead of raising into the caller -- this is the
    guarantee that lets record_auth_failure() be called from the request
    path without risk of breaking a real request."""
    from pathlib import Path

    monkeypatch.setattr(auth_stats, "_STATS_FILE", Path("\x00bad\x00path\x00auth_stats.json"))
    auth_stats.write_snapshot()  # must not raise


def test_get_summary_never_raises_even_if_tracker_broken(monkeypatch):
    class _Exploding:
        def summary(self, ts=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(auth_stats, "_TRACKER", _Exploding())
    result = auth_stats.get_summary()
    assert result == {"auth_fail_10m": 0, "auth_fail_last_ts": None}


def test_record_auth_failure_never_raises_even_if_tracker_broken(monkeypatch, tmp_path):
    class _Exploding:
        def record(self, ts=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(auth_stats, "_TRACKER", _Exploding())
    monkeypatch.setattr(auth_stats, "_STATS_FILE", tmp_path / "auth_stats.json")
    auth_stats.record_auth_failure()  # must not raise
