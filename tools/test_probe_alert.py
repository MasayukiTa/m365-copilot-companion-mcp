# -*- coding: utf-8 -*-
"""The alert must fire on 2 consecutive failures, name the hop, and name the cause.

MEASURED 2026-11-11 (the incident this closes): the gateway was down for hours and the only
monitoring did GET /health, which never fails the way the real outage did. When an outage is
visible at all it must say WHICH hop broke and the LIKELY cause -- otherwise the reader clears
it and the next one too (the same lesson as tools/auth_stats.py's rejection log). These tests
pin the three things goal C promises: (1) two consecutive failures within the window raise one
alert and not before; (2) the alert names the failing hop and a cause an operator can act on;
(3) the durable record carries no credential and is bounded.

These tests are hermetic: the pure tracker needs no I/O, and record_probe_result's toast is
inert under pytest by construction (tools/notify_ops.notify_desktop checks PYTEST_CURRENT_TEST
and MCP_SUPPRESS_GUI), so nothing opens a window on a runner. The durable sidecar is redirected
to a tmp file so no test ever writes into the operator's live .fleet record.
"""
from __future__ import annotations

import json

import pytest

import tools.probe_alert as PA


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # redirect the durable record so a test never touches the operator's .fleet file
    monkeypatch.setattr(PA, "_ALERTS_FILE", tmp_path / "probe_alerts.jsonl")
    # a fresh tracker per test: no streak leaks between cases
    monkeypatch.setattr(PA, "_TRACKER", PA.ProbeAlertTracker())
    return tmp_path


def test_one_failure_does_not_alert():
    """A single failed beat is normal noise (a restart in progress, one slow beat). The alert
    must not fire on the first failure -- that is exactly the false alarm that teaches people
    to ignore it."""
    out = PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=1000.0)
    assert out is None
    assert PA.recent_alerts() == []


def test_two_consecutive_failures_fire_one_alert_naming_hop_and_cause():
    """THE PROMISE. Two consecutive failures of the same hop within the window raise the alert,
    and the alert names the hop and a cause -- here, the public relay having no host."""
    assert PA.record_probe_result(PA.HOP_PUBLIC, ok=False, kind=PA.KIND_NO_HOST, ts=1000.0) is None
    out = PA.record_probe_result(PA.HOP_PUBLIC, ok=False, kind=PA.KIND_NO_HOST, ts=1060.0)
    assert out is not None
    assert out["hop"] == PA.HOP_PUBLIC
    assert out["kind"] == PA.KIND_NO_HOST
    assert out["consecutive"] == 2
    # the cause names what to go look at, in the operator's language
    assert "devtunnel" in out["cause_ja"]
    title, body = PA.alert_text(out)
    assert PA.HOP_PUBLIC in title
    assert out["cause_ja"] in body


def test_local_down_cause_points_at_main_py():
    """The local hop failing twice is the hung-main.py shape; the cause must say so, not just
    'down'."""
    PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=1.0)
    out = PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=61.0)
    assert out is not None
    assert "main.py" in out["cause_ja"]


def test_alert_fires_once_per_streak_not_every_beat():
    """After the threshold is crossed, further failures in the SAME streak must not toast again
    every 60 s -- that is a notification storm, the failure open_authority_dashboard's cooldown
    was added for. One alert per streak."""
    assert PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=1.0) is None
    assert PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=61.0) is not None
    # third and fourth consecutive failures: still failing, but no NEW alert
    assert PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=121.0) is None
    assert PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=181.0) is None
    assert len(PA.recent_alerts()) == 1


def test_success_resets_the_streak_and_rearms_the_alert():
    """A recovered hop clears the streak; a later pair of failures is a new outage and alerts
    again."""
    PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=1.0)
    assert PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=61.0) is not None
    PA.record_probe_result(PA.HOP_LOCAL, ok=True, ts=121.0)   # recovered
    assert PA._TRACKER.streak(PA.HOP_LOCAL) == 0
    PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=181.0)
    out = PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=241.0)
    assert out is not None
    assert len(PA.recent_alerts()) == 2


def test_two_failures_far_apart_do_not_chain():
    """Two failures more than the window apart are not a streak of two -- a stalled probe loop
    must not satisfy the condition with two unrelated failures. The second starts a fresh streak
    of one, so no alert."""
    assert PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=1.0) is None
    # 500 s later, well beyond the 120 s window
    assert PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=501.0) is None
    assert PA.recent_alerts() == []


def test_the_two_hops_count_independently():
    """A local failure and a public failure are not two failures of one hop. Each hop needs its
    own two consecutive failures before it alerts."""
    assert PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=1.0) is None
    assert PA.record_probe_result(PA.HOP_PUBLIC, ok=False, kind=PA.KIND_NO_HOST, ts=2.0) is None
    assert PA.recent_alerts() == []
    # now the local hop fails a second time -> only the local alert
    out = PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=3.0)
    assert out is not None and out["hop"] == PA.HOP_LOCAL


def test_durable_record_carries_no_credential_and_only_safe_fields():
    """A file recording failures is the last place a key should land. The row carries only the
    hop, kind, cause, count and ts -- and this module must never gain a way to receive a key."""
    PA.record_probe_result(PA.HOP_PUBLIC, ok=False, kind=PA.KIND_AUTH, ts=1.0)
    PA.record_probe_result(PA.HOP_PUBLIC, ok=False, kind=PA.KIND_AUTH, ts=61.0)
    rows = PA.recent_alerts()
    assert len(rows) == 1
    assert set(rows[0]) == {"ts", "hop", "kind", "consecutive", "cause_ja"}, rows[0]
    import inspect
    src = inspect.getsource(PA)
    assert "authorization" not in src.lower(), "this module gained a way to see the credential"
    assert "require_api_key" not in src, "this module must not resolve the API key"


def test_the_record_is_bounded_and_keeps_the_recent_half():
    """Written from the probe loop on a machine whose disk has been filled to zero once by an
    unbounded log. On overflow the recent half survives -- the question is about the alert that
    just fired."""
    PA._ALERTS_FILE.write_text("x" * (PA._ALERTS_MAX_BYTES + 10) + "\n", encoding="utf-8")
    for i in range(6):
        # a fresh alert each time: fail twice, then succeed to re-arm
        PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=float(i * 1000))
        PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=float(i * 1000 + 60))
        PA.record_probe_result(PA.HOP_LOCAL, ok=True, ts=float(i * 1000 + 120))
    assert PA._ALERTS_FILE.stat().st_size <= PA._ALERTS_MAX_BYTES
    assert "xxxx" not in PA._ALERTS_FILE.read_text(encoding="utf-8"), \
        "the trimmed half is still there; the file is not actually bounded"


def test_recording_cannot_break_the_probe_loop():
    """It is bookkeeping on the probe loop. An undirectable sidecar path must not raise into the
    caller."""
    PA._ALERTS_FILE = PA._ALERTS_FILE.parent / "no" / "such" / "dir" / "x.jsonl"
    PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=1.0)
    PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind=PA.KIND_LOCAL_DOWN, ts=61.0)  # must not raise


def test_unknown_kind_degrades_to_error_not_crash():
    """An unexpected classification label from the probe must never crash the mapping; it reads
    as a generic error with a cause rather than taking down the loop."""
    PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind="something_new", ts=1.0)
    out = PA.record_probe_result(PA.HOP_LOCAL, ok=False, kind="something_new", ts=61.0)
    assert out is not None
    assert out["kind"] == PA.KIND_ERROR
    assert out["cause_ja"]


def test_probe_tick_records_results_and_exposes_alerts(monkeypatch):
    from tools import mcp_handshake_probe as probe

    monkeypatch.setattr(probe, "handshake", lambda url, key: {
        "initialize_status": 404 if url != probe.LOCAL_URL else 200,
        "initialize_latency_s": 1.0})
    monkeypatch.setattr(probe, "_public_url", lambda: "https://example.test/mcp")
    monkeypatch.setattr(probe, "read_login_state", lambda: {})
    monkeypatch.setattr(probe, "probe_c2c_entrance", lambda: {})
    probe.run_tick("unused", "unused", {})
    record, _ = probe.run_tick("unused", "unused", {})
    assert record["recent_probe_alerts"][-1]["kind"] == PA.KIND_NO_HOST
    assert record["recent_probe_alerts"][-1]["hop"] == PA.HOP_PUBLIC
