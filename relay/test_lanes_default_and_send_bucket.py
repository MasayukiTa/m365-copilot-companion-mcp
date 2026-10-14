# -*- coding: utf-8 -*-
"""1000 lanes by default, RAM as a metric not a gate, and the socket send pacing as a rate.

Pure decisions with synthetic inputs; no browser.
"""
import os

import pytest

from relay import fleet_runner as FR
from relay import relay_fleet as RF
from relay.send_bucket import TokenBucket

NOW = 10_000.0


@pytest.fixture(autouse=True)
def _clean():
    RF._reset_admission_pacing()
    yield
    RF._reset_admission_pacing()


# ── lanes ─────────────────────────────────────────────────────────────────────────────────

def test_the_default_lane_count_is_a_thousand_everywhere():
    assert FR.LANES_DEFAULT == 1000
    assert FR.DEFAULT_MAX_CONCURRENT == 1000
    assert FR.AUTOSCALE_CEILING_DEFAULT == 1000
    assert FR.TABS_BOUNDS[1] >= 1000
    import inspect
    assert inspect.signature(RF.auto_concurrency).parameters["hard_cap"].default == 1000


def test_the_automatic_lane_count_ignores_free_ram(monkeypatch):
    monkeypatch.setattr(RF, "avail_phys_mb", lambda: 10.0)           # nothing free at all
    assert RF.RAM_GOVERNS_CONCURRENCY is False
    assert RF.auto_concurrency(1000) == 1000
    assert RF.auto_concurrency(5) == 5


def test_the_old_ram_arithmetic_is_still_there_when_asked_for(monkeypatch):
    monkeypatch.setattr(RF, "RAM_GOVERNS_CONCURRENCY", True)
    monkeypatch.setattr(RF, "avail_phys_mb", lambda: 1372.0)
    assert RF.auto_concurrency(1000, per_tab_mb=400, headroom_mb=512) == 2


def test_side_pages_are_not_held_back_by_ram(monkeypatch):
    monkeypatch.setattr(RF, "avail_phys_mb", lambda: 10.0)
    assert RF.ram_room_for_tab() is True
    monkeypatch.setattr(RF, "RAM_GOVERNS_CONCURRENCY", True)
    assert RF.ram_room_for_tab() is False


def test_a_stale_settings_value_does_not_set_the_start_up_cap(monkeypatch, capsys):
    monkeypatch.setattr(FR, "_settings_int", lambda key, default: {"maxtabs": 3,
                                                                    "autoscale_max": 30}.get(key, default))
    monkeypatch.setattr(FR, "HONOR_SETTINGS_LANES", False)
    assert FR.lane_setting("maxtabs") == 1000
    assert FR.lane_setting("autoscale_max") == 1000
    out = capsys.readouterr().out
    assert "ignoring settings.txt maxtabs=3" in out and "autoscale_max=30" in out


def test_settings_can_be_honoured_only_by_opting_in(monkeypatch):
    monkeypatch.setattr(FR, "_settings_int", lambda key, default: 30)
    monkeypatch.setattr(FR, "HONOR_SETTINGS_LANES", True)
    assert FR.lane_setting("autoscale_max") == 30


def test_the_panel_clamp_and_the_command_bounds_allow_a_thousand():
    assert FR._goal_item_error({"text": "x"}) == ""
    assert FR.TABS_BOUNDS == (1, 1000)


def test_the_cap_follows_the_ceiling_not_free_ram(monkeypatch):
    """The autoscale branch used to set the cap from ram_target_cap; now it is the ceiling."""
    import inspect
    src = inspect.getsource(RF.run_relay_fleet)
    assert "asc_on and not RAM_GOVERNS_CONCURRENCY" in src
    assert "mc_box[0] = max(1, int(ceiling))" in src


def test_socket_goals_skip_the_tab_budget():
    import inspect
    src = inspect.getsource(RF.run_relay_fleet)
    assert "if not _candidate_socket and not admits_another_tab(" in src


# ── the send bucket ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def open_gate(monkeypatch):
    monkeypatch.setattr(RF, "rate_headroom_ok", lambda now=None: (True, ""))
    monkeypatch.setattr(RF, "rate_ceiling", lambda default=None: 100.0)


def _sweep(now):
    sent = 0
    while RF.socket_send_due(now) and sent < 1000:
        RF.note_socket_sent(now)
        sent += 1
    return sent


def test_one_sweep_sends_many_ready_goals_up_to_the_burst(open_gate):
    assert _sweep(NOW) == int(RF.SOCKET_SEND_BURST)       # the old gate allowed exactly one


def test_the_sustained_rate_is_the_configured_rate_not_one_per_sweep(open_gate):
    sent = 0
    for i in range(60):                                   # a sweep every 5 s for 5 minutes
        sent += _sweep(NOW + i * 5)
    assert sent >= RF.SOCKET_SEND_RPM * 4.5               # ~30/min, where the gate gave ~2/min
    assert sent <= RF.SOCKET_SEND_RPM * 5 + RF.SOCKET_SEND_BURST


def test_the_configured_ceiling_is_a_hard_maximum(monkeypatch):
    monkeypatch.setattr(RF, "rate_ceiling", lambda default=None: 10.0)
    assert RF.socket_send_rate_now(NOW)[0] == 10.0
    monkeypatch.setattr(RF, "rate_ceiling", lambda default=None: 0.0)
    assert RF.socket_send_rate_now(NOW)[0] == RF.SOCKET_SEND_RPM


def test_the_default_rate_starts_well_under_the_cloud_limit():
    # default raised 30 -> 100 on 2026-10-14 (owner/main order); the rate actually used is still
    # min(this, rate_ceiling_rpm), asserted in test_the_configured_ceiling_is_a_hard_maximum.
    assert RF.SOCKET_SEND_RPM <= 100 and RF.SOCKET_SEND_BURST <= 8


def test_a_throttle_cuts_rate_and_burst(open_gate):
    RF.note_upstream_throttle(now=NOW)
    rate, burst = RF.socket_send_rate_now(NOW + 1)
    assert rate < RF.SOCKET_SEND_RPM and burst == 1.0
    assert _sweep(NOW + 1) == 1
    assert RF.socket_send_rate_now(NOW + RF.ADMIT_THROTTLE_MEMORY_S + 5) == (
        RF.SOCKET_SEND_RPM, RF.SOCKET_SEND_BURST)


def test_the_measured_quota_still_gates_even_with_tokens(monkeypatch):
    monkeypatch.setattr(RF, "rate_headroom_ok", lambda now=None: (False, "held"))
    assert RF.socket_send_due(NOW) is False


def test_asking_does_not_spend(open_gate):
    assert RF.socket_send_due(NOW) and RF.socket_send_due(NOW)
    assert _sweep(NOW) == int(RF.SOCKET_SEND_BURST)


def test_bucket_unit():
    b = TokenBucket()
    assert b.available(0, 60, 3) == 3
    assert b.take(0, 60, 3, 2) and b.available(0, 60, 3) == 1
    assert not b.take(0, 60, 3, 2)
    assert b.available(1, 60, 3) == 2
    assert b.available(1000, 60, 3) == 3


def test_the_pacing_reason_is_the_one_the_cockpit_shows():
    import inspect
    src = inspect.getsource(RF.RelayWorker._begin_send)
    assert "socket_send_due()" in src and "note_socket_sent()" in src


def test_the_emergency_override_is_marked_and_announced_at_run_start():
    """The owner's rule: this is temporary and must say so, in the code and in the run's log."""
    import inspect
    assert FR.EMERGENCY_OVERRIDE.startswith("EMERGENCY 2026-10-14 (owner order)")
    main_src = inspect.getsource(FR.main)
    assert "emergency override active" in main_src
    for obj in (RF, FR):
        text = open(obj.__file__, encoding="utf-8").read()
        assert text.count("EMERGENCY 2026-10-14 (owner order) -- temporary, must be replaced by the "
                          "root-cause design") >= 1


# ── watchdog evidence and log stamps ──────────────────────────────────────────────────────

def test_a_stall_writes_all_thread_stacks_and_the_stall_facts(tmp_path):
    FR._write_stall_diagnostic(str(tmp_path), {"updated": 1000.0, "workers": [
        {"name": "w0", "status": "verifying", "eval_busy_until": 0}]}, 200.0, True, "wedged",
        now=1200.0)
    body = (tmp_path / "stall_diagnostics.log").read_text(encoding="utf-8")
    assert "STALL 200s" in body and "will_reset=True" in body
    assert "SWEEP/MAIN THREAD" in body and "faulthandler dump" in body
    assert "status.json updated=" in body and "worker w0 status=verifying" in body


def test_log_lines_get_a_clock_and_keep_a_leading_newline():
    from relay.logstamp import stamp
    s = stamp("\n[watchdog] x")
    assert s.startswith("\n") and "[watchdog] x" in s and s[1:9].count(":") == 2
