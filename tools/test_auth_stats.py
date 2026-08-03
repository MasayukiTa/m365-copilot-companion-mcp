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
# 1b. Per-IP breakdown (request origin): record(ip=...) / summary(include_ip_breakdown=True)
# ===========================================================================


def test_summary_default_never_includes_ip_breakdown():
    """summary() with no args must keep returning exactly the original
    two-key shape -- this is what get_summary()/the public /health route
    consume, and it must never grow the per-IP data."""
    t = auth_stats.AuthFailureTracker(window_s=600.0)
    t.record(ts=1.0, ip="1.2.3.4")
    s = t.summary(ts=2.0)
    assert set(s.keys()) == {"auth_fail_10m", "auth_fail_last_ts"}


def test_per_ip_counts_within_window():
    t = auth_stats.AuthFailureTracker(window_s=600.0)
    t.record(ts=1.0, ip="1.1.1.1")
    t.record(ts=2.0, ip="1.1.1.1")
    t.record(ts=3.0, ip="2.2.2.2")
    s = t.summary(ts=4.0, include_ip_breakdown=True)
    assert s["auth_fail_by_ip"] == {"1.1.1.1": 2, "2.2.2.2": 1}
    # Existing fields untouched by asking for the breakdown too.
    assert s["auth_fail_10m"] == 3
    assert s["auth_fail_last_ts"] == 3.0


def test_per_ip_window_pruning_matches_global_pruning():
    """Per-IP counts must prune on the same window as the global count, not
    just accumulate forever."""
    t = auth_stats.AuthFailureTracker(window_s=600.0)
    t.record(ts=0.0, ip="1.1.1.1")     # will age out
    t.record(ts=100.0, ip="1.1.1.1")   # survives
    t.record(ts=500.0, ip="2.2.2.2")   # survives
    s = t.summary(ts=650.0, include_ip_breakdown=True)  # cutoff = 50
    assert s["auth_fail_by_ip"] == {"1.1.1.1": 1, "2.2.2.2": 1}

    # Push further out so both IPs fully age out and their slots are freed.
    s2 = t.summary(ts=2000.0, include_ip_breakdown=True)
    assert s2["auth_fail_by_ip"] == {}


def test_unknown_or_missing_ip_uses_placeholder_bucket():
    t = auth_stats.AuthFailureTracker(window_s=600.0)
    t.record(ts=1.0)             # ip omitted entirely
    t.record(ts=2.0, ip=None)    # ip explicitly None
    t.record(ts=3.0, ip="")      # ip explicitly empty
    s = t.summary(ts=4.0, include_ip_breakdown=True)
    assert s["auth_fail_by_ip"] == {"": 3}


def test_record_with_ip_never_raises_on_bad_ip_type():
    t = auth_stats.AuthFailureTracker(window_s=600.0)

    class _Unstringable:
        def __str__(self):
            raise RuntimeError("boom")

    t.record(ts=1.0, ip=_Unstringable())  # must not raise
    s = t.summary(ts=2.0, include_ip_breakdown=True)
    assert s["auth_fail_by_ip"] == {"": 1}


def test_ip_cap_folds_overflow_into_other_bucket():
    """With a small ip_cap, IPs beyond the cap must fold into the "__other__"
    bucket instead of growing the per-IP dict without bound."""
    t = auth_stats.AuthFailureTracker(window_s=600.0, ip_cap=2)
    t.record(ts=1.0, ip="1.1.1.1")
    t.record(ts=2.0, ip="2.2.2.2")
    # Cap (2) already reached by two distinct real IPs above; a third
    # distinct IP must NOT get its own bucket.
    t.record(ts=3.0, ip="3.3.3.3")
    t.record(ts=4.0, ip="4.4.4.4")
    s = t.summary(ts=5.0, include_ip_breakdown=True)
    assert s["auth_fail_by_ip"] == {
        "1.1.1.1": 1,
        "2.2.2.2": 1,
        "__other__": 2,
    }
    # The overflow does not affect the plain global count.
    assert s["auth_fail_10m"] == 4


def test_ip_cap_slot_frees_up_once_events_age_out():
    """Once a tracked IP's events fully age out of the window, its slot is
    freed and a later, previously-unseen IP can claim its own bucket again --
    the cap self-heals rather than permanently starving new addresses."""
    t = auth_stats.AuthFailureTracker(window_s=10.0, ip_cap=1)
    t.record(ts=0.0, ip="1.1.1.1")
    # Still within window at ts=5: cap is full, "2.2.2.2" must overflow.
    t.record(ts=5.0, ip="2.2.2.2")
    s_mid = t.summary(ts=5.0, include_ip_breakdown=True)
    assert s_mid["auth_fail_by_ip"] == {"1.1.1.1": 1, "__other__": 1}

    # By ts=25, "1.1.1.1"'s only event (ts=0.0) is long out of the 10s window,
    # so its slot should free up for the next distinct IP.
    t.record(ts=25.0, ip="3.3.3.3")
    s_late = t.summary(ts=26.0, include_ip_breakdown=True)
    assert s_late["auth_fail_by_ip"] == {"3.3.3.3": 1}


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
    assert "auth_fail_by_ip" not in summary, (
        "get_summary() feeds the public /health route and must never carry "
        "the per-IP breakdown -- that is sidecar-only, see write_snapshot()"
    )

    assert stats_file.is_file(), "write_snapshot must have created .fleet/auth_stats.json"
    on_disk = json.loads(stats_file.read_text(encoding="utf-8"))
    # The sidecar is intentionally richer than get_summary(): it also carries
    # the per-IP breakdown (recorded here with no ip -> the "" placeholder
    # bucket), which must never be surfaced through get_summary()/health.
    assert on_disk["auth_fail_10m"] == summary["auth_fail_10m"]
    assert on_disk["auth_fail_last_ts"] == summary["auth_fail_last_ts"]
    assert on_disk["auth_fail_by_ip"] == {"": 1}


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
