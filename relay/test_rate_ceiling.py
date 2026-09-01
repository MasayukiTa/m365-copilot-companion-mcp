"""Admission holds at the user's rate ceiling instead of feeding a queue that is being refused.

At the ceiling, admitting another worker does not get more work through -- it gets the same
work refused, and a refusal costs a turn on the way in as well as a retry on the way out. So
the fleet waits at the line and admits the moment the trailing minute drops below it.
"""
import pytest

from relay import relay_fleet as F


@pytest.fixture(autouse=True)
def _clean():
    F._reset_admission_pacing()
    yield
    F._reset_admission_pacing()


def _rpm(monkeypatch, value):
    monkeypatch.setattr(F, "current_rpm", lambda now=None: value)


def test_at_the_ceiling_nothing_is_admitted(monkeypatch):
    monkeypatch.setattr(F, "rate_ceiling", lambda default=None: 100.0)
    _rpm(monkeypatch, 100.0)
    ok, why = F.rate_headroom_ok()
    assert ok is False
    assert "holding" in why
    assert F.admission_is_due(now=1e9) is False


def test_over_the_ceiling_nothing_is_admitted(monkeypatch):
    monkeypatch.setattr(F, "rate_ceiling", lambda default=None: 40.0)
    _rpm(monkeypatch, 55.0)
    assert F.rate_headroom_ok()[0] is False


def test_one_below_the_ceiling_admits_again(monkeypatch):
    # The whole point of waiting: the moment it drops, work resumes without anyone intervening.
    monkeypatch.setattr(F, "rate_ceiling", lambda default=None: 100.0)
    _rpm(monkeypatch, 99.0)
    assert F.rate_headroom_ok()[0] is True
    assert F.admission_is_due(now=1e9) is True


def test_a_lower_ceiling_binds_before_the_published_one(monkeypatch):
    # The reason this is settable at all. Microsoft publishes 100 RPM, but refusals arriving
    # while headroom looks comfortable mean the binding limit is not the published one.
    monkeypatch.setattr(F, "rate_ceiling", lambda default=None: 30.0)
    _rpm(monkeypatch, 45.0)
    assert F.rate_headroom_ok()[0] is False


def test_a_zero_ceiling_disables_the_gate(monkeypatch):
    monkeypatch.setattr(F, "rate_ceiling", lambda default=None: 0.0)
    _rpm(monkeypatch, 100000.0)
    assert F.rate_headroom_ok()[0] is True


def test_an_unreadable_meter_does_not_stall_the_fleet(monkeypatch):
    # FAILS OPEN, deliberately, and this is the one place in this change where that is right:
    # the gate is an optimisation against wasted refusals, not a safety property. A missing
    # meter file must not be able to halt every run on the machine indefinitely.
    monkeypatch.setattr(F, "rate_ceiling", lambda default=None: 100.0)
    _rpm(monkeypatch, None)
    assert F.rate_headroom_ok()[0] is True
    assert F.admission_is_due(now=1e9) is True


def test_a_quiet_minute_is_not_confused_with_a_missing_meter(monkeypatch):
    # Zero and None are different answers: one means there is room, the other means we cannot
    # tell. Collapsing them is how a broken meter comes to look like comfortable headroom.
    import relay.quota_meter as Q
    monkeypatch.setattr(Q, "snapshot", lambda now=None: {"rpm": 0, "measured": False})
    F._reset_admission_pacing()
    assert F.current_rpm(now=1.0) is None
    monkeypatch.setattr(Q, "snapshot", lambda now=None: {"rpm": 0, "measured": True})
    F._reset_admission_pacing()
    assert F.current_rpm(now=2.0) == 0.0


def test_the_ceiling_defaults_to_the_published_limit(monkeypatch):
    from relay.quota_meter import LIMIT_RPM
    monkeypatch.setattr(F, "_settings_float", None, raising=False)
    # No settings.txt key present -> the documented 100 RPM.
    monkeypatch.setattr("relay.fleet_runner._settings_float",
                        lambda key, default: default)
    assert F.rate_ceiling() == LIMIT_RPM


def test_the_user_ceiling_is_read_from_the_cockpit_settings(monkeypatch):
    monkeypatch.setattr("relay.fleet_runner._settings_float",
                        lambda key, default: 42.0 if key == "rate_ceiling_rpm" else default)
    assert F.rate_ceiling() == 42.0


def test_the_meter_is_not_read_once_per_admission_sweep(monkeypatch):
    # Admission asks about once a second; the meter is an hour of turns read off disk. Without
    # the cache the gate becomes the expensive part of the loop it is protecting.
    calls = []
    import relay.quota_meter as Q
    monkeypatch.setattr(Q, "snapshot",
                        lambda now=None: calls.append(now) or {"rpm": 1, "measured": True})
    F._reset_admission_pacing()
    for i in range(20):
        F.current_rpm(now=1000.0 + i * 0.05)
    assert len(calls) == 1, calls


def test_the_cache_does_expire(monkeypatch):
    calls = []
    import relay.quota_meter as Q
    monkeypatch.setattr(Q, "snapshot",
                        lambda now=None: calls.append(now) or {"rpm": 1, "measured": True})
    F._reset_admission_pacing()
    F.current_rpm(now=1000.0)
    F.current_rpm(now=1000.0 + F._RATE_CACHE_TTL_S + 0.1)
    assert len(calls) == 2, calls
