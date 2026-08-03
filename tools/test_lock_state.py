"""A lock refusal must be detectable without relying on how the agent phrased it.

The incident: the relay decides whether to auto-unlock by looking for the server's
literal error ("[locked client IP: ...]") in the agent's reply. The operator
discipline injected into every turn tells the agent to write "淡々と事実とタスク
結果のみ", so it summarises instead -- "unlock パスワード欠如で確定。STUCK: unlock
パスワード未提供。" -- and no marker appears. Detection missed, the generic retry
nudge ran in place of the unlock injection, and the run STUCKed asking a human for
a password that was already in .env.

Run: pytest -q tools/test_lock_state.py
"""
from __future__ import annotations

import json

import pytest

from tools import lock_state


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_state, "_STATE_FILE", tmp_path / "lock_state.json")
    yield


def test_no_record_means_not_locked():
    assert lock_state.read_state() == {}
    assert lock_state.locked_recently() is False


def test_a_refusal_is_visible_immediately():
    lock_state.record_locked("203.0.113.7", "[locked client IP: '203.0.113.7'] ...")
    assert lock_state.locked_recently() is True
    assert lock_state.read_state()["client_ip"] == "203.0.113.7"


def test_an_old_refusal_does_not_colour_a_later_turn():
    """Freshness is what keeps the fallback honest."""
    lock_state.record_locked("203.0.113.7", ts=1_000.0)
    assert lock_state.locked_recently(within_sec=180.0, now=1_100.0) is True
    assert lock_state.locked_recently(within_sec=180.0, now=1_500.0) is False


def test_unlock_clears_the_record():
    lock_state.record_locked("203.0.113.7")
    lock_state.clear()
    assert lock_state.locked_recently() is False


def test_corrupt_or_unreadable_state_is_treated_as_no_lock():
    lock_state._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_state._STATE_FILE.write_text("{not json", encoding="utf-8")
    assert lock_state.read_state() == {}
    assert lock_state.locked_recently() is False


def test_a_record_without_a_usable_timestamp_is_not_a_lock():
    lock_state._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    for bad in ({"ts": "later"}, {"ts": 0}, {}):
        lock_state._STATE_FILE.write_text(json.dumps(bad), encoding="utf-8")
        assert lock_state.locked_recently() is False


def test_detail_and_ip_are_bounded():
    lock_state.record_locked("x" * 500, "y" * 5000)
    state = lock_state.read_state()
    assert len(state["client_ip"]) <= 64
    assert len(state["detail"]) <= 200


def test_recording_never_raises_even_when_the_path_is_unusable(tmp_path, monkeypatch):
    """Request handling must never break because this sidecar could not be written."""
    monkeypatch.setattr(lock_state, "_STATE_FILE", tmp_path / "nope" / "\0bad" / "s.json")
    lock_state.record_locked("203.0.113.7")      # must not raise
    lock_state.clear()                            # must not raise


# --- turn scoping ------------------------------------------------------------
# CI caught the reason this matters. tools/security.py records a refusal into the
# real sidecar, so one test calling require_unlocked() left a fresh record behind;
# a later test's ordinary refusal reply ("I cannot assist with that request") was
# then read as a lock, and two unrelated resilience tests failed. "Was anything
# refused lately" is too broad to judge one turn by.


def test_only_a_refusal_from_this_turn_counts():
    sent_at = 1_000.0
    lock_state.record_locked("203.0.113.7", ts=sent_at + 1)
    assert lock_state.locked_since(sent_at, now=sent_at + 2) is True


def test_a_refusal_from_before_the_turn_is_ignored():
    """The exact CI contamination: an earlier, unrelated call left the record."""
    sent_at = 1_000.0
    lock_state.record_locked("203.0.113.7", ts=sent_at - 5)
    assert lock_state.locked_since(sent_at, now=sent_at + 2) is False
    # ...while the broad query would still have said yes, which is the bug.
    assert lock_state.locked_recently(within_sec=180.0, now=sent_at + 2) is True


def test_no_boundary_means_no_detection():
    """A caller that cannot say when its turn started gets nothing, not everything."""
    lock_state.record_locked("203.0.113.7")
    for bad in (0.0, -1.0, None, "x"):
        assert lock_state.locked_since(bad) is False


def test_freshness_still_bounds_a_scoped_query():
    sent_at = 1_000.0
    lock_state.record_locked("203.0.113.7", ts=sent_at + 1)
    assert lock_state.locked_since(sent_at, now=sent_at + 10_000) is False
