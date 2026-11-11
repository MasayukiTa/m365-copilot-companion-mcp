# -*- coding: utf-8 -*-
"""A tunnel-down alert is worthless unless it says WHICH cause and what to do.

The incident showed that tunnel failures need an actionable notification.
The four failure causes (not logged in / local server down / no host / public path down) each
need a DIFFERENT human action, so the alert that fires must name the one it is and carry its
remedy. These tests pin that: the classifier picks the right cause, the toast body names it,
the login case says to use a VISIBLE console (never device-code), and no credential is ever
put into an alert. The notify sink is injected, so nothing here pops a real toast -- and the
real default sink is inert under pytest by construction anyway.

Portable: no Windows-only paths or separators are asserted; runs on the Linux CI runner.
"""
from __future__ import annotations

import tools.tunnel_alert as T


def _obs(logged_in=True, host_connections=1, local_health_ok=True, public_health_ok=True):
    return T.ProbeObservation(
        logged_in=logged_in,
        host_connections=host_connections,
        local_health_ok=local_health_ok,
        public_health_ok=public_health_ok,
    )


def test_a_healthy_reading_raises_nothing():
    """The quiet case. All four facts good -> no cause, no alert. An alert that fires when
    nothing is wrong is the fastest way to teach the reader to ignore it."""
    assert _obs().healthy() is True
    assert T.classify_probe(_obs()) is None
    raised = T.raise_tunnel_alert(_obs(), notify=_recording_notify())
    assert raised is None


def test_login_is_decided_before_everything_else():
    """THE 2026-11-11 ROOT. When not logged in the supervisor pauses hosting, so 'no host' is a
    symptom, not the cause. Even with host=0 and local down, the cause reported must be login --
    otherwise the operator restarts a tunnel that cannot be hosted until they sign in."""
    obs = _obs(logged_in=False, host_connections=0, local_health_ok=False, public_health_ok=False)
    assert T.classify_probe(obs) == T.CAUSE_NOT_LOGGED_IN


def test_local_server_down_outranks_host_and_public():
    """Logged in but main.py's /health is down -> nothing downstream can be up, so the
    actionable cause is the local server, not the public path."""
    obs = _obs(local_health_ok=False, host_connections=0, public_health_ok=False)
    assert T.classify_probe(obs) == T.CAUSE_LOCAL_SERVER_DOWN


def test_no_host_when_connections_are_zero():
    """Logged in, local up, but host connections = 0 -> the public URL cannot reach this PC."""
    obs = _obs(host_connections=0)
    assert T.classify_probe(obs) == T.CAUSE_NO_HOST


def test_unknown_host_count_is_not_treated_as_zero():
    """host_connections=None means the CLI was unreachable, NOT that there is no host. With
    login/local/public all fine, an unknown count is not an alertable failure (the 2026-11-04
    class of defect: do not conflate 'unknown' with 'zero')."""
    assert T.classify_probe(_obs(host_connections=None)) is None
    # But if the public path is also down, the actionable fact is public_path_down.
    obs = _obs(host_connections=None, public_health_ok=False)
    assert T.classify_probe(obs) == T.CAUSE_PUBLIC_PATH_DOWN


def test_public_path_down_when_only_the_public_url_fails():
    """Logged in, hosted, local up, but the public URL fails -> the relay path is the suspect."""
    obs = _obs(public_health_ok=False)
    assert T.classify_probe(obs) == T.CAUSE_PUBLIC_PATH_DOWN


def test_every_cause_builds_an_alert_that_names_itself_and_a_remedy():
    """Each cause's toast must be self-contained: the title or body names the cause, and the
    body carries a fix the operator can act on from the text alone."""
    expect_word = {
        T.CAUSE_NOT_LOGGED_IN: "logged in",
        T.CAUSE_NO_HOST: "host",
        T.CAUSE_LOCAL_SERVER_DOWN: "local",
        T.CAUSE_PUBLIC_PATH_DOWN: "public",
    }
    for cause in T.ALL_CAUSES:
        alert = T.build_alert(cause)
        text = (alert.title + "\n" + alert.body).lower()
        assert expect_word[cause] in text, (cause, alert.title)
        assert "fix:" in alert.body.lower(), cause
        assert alert.cause == cause


def test_the_login_alert_demands_a_visible_console_and_forbids_device_code():
    """Goal (c)'s notification half: when interactive login is needed the alert must SAY to open
    it in a visible console, and must warn off device-code (blocked by this tenant's Entra)."""
    alert = T.build_alert(T.CAUSE_NOT_LOGGED_IN)
    assert alert.needs_visible_console_login is True
    low = alert.body.lower()
    assert "visible console" in low
    assert "device-code" in low or "device code" in low
    # Only the login cause needs the console; the others do not.
    for other in (T.CAUSE_NO_HOST, T.CAUSE_LOCAL_SERVER_DOWN, T.CAUSE_PUBLIC_PATH_DOWN):
        assert T.build_alert(other).needs_visible_console_login is False


def test_raise_passes_cause_body_and_launch_to_the_sink():
    """The connecting face: a failure must actually reach the notify sink, with the cause named
    and a click target set."""
    rec = _recording_notify()
    out = T.raise_tunnel_alert(_obs(logged_in=False), notify=rec)
    assert out is not None
    assert out["cause"] == T.CAUSE_NOT_LOGGED_IN
    assert out["needs_visible_console_login"] == "1"
    assert len(rec.calls) == 1
    title, body, kwargs = rec.calls[0]
    assert "logged in" in (title + body).lower()
    assert kwargs.get("launch")  # a clicked toast lands somewhere


def test_no_credential_or_token_appears_in_any_alert():
    """A toast is the last place a key should land. The module is given booleans/counts, never
    a token, and nothing it emits may look like one. A public devtunnels URL is not a secret."""
    banned = ("authorization", "bearer", "password", "secret", "token", "access_token")
    for cause in T.ALL_CAUSES:
        alert = T.build_alert(cause, public_url="https://x0wx6c3x-8000.jpe1.devtunnels.ms/health")
        blob = (alert.title + alert.body).lower()
        for b in banned:
            assert b not in blob, (cause, b)


def test_build_alert_rejects_an_unknown_cause():
    """A typo'd cause must fail loudly, not emit a blank toast."""
    import pytest

    with pytest.raises(ValueError):
        T.build_alert("not_a_real_cause")


class _recording_notify:
    """A fake notify sink that records its calls instead of popping a toast."""

    def __init__(self):
        self.calls = []

    def __call__(self, title, body, **kwargs):
        self.calls.append((title, body, kwargs))
        return "recorded"


def test_probe_tick_raises_once_and_rearms_after_recovery(monkeypatch):
    from tools import mcp_handshake_probe as probe
    from scripts import tunnel_login_state as login

    state = {"login": login.LOGIN_NONE}
    shown = []
    monkeypatch.setattr(probe, "_last_tunnel_cause", None)
    monkeypatch.setattr(probe, "handshake", lambda url, key: {
        "initialize_status": 200, "initialize_latency_s": 1.0})
    monkeypatch.setattr(probe, "_public_url", lambda: "https://example.test/mcp")
    monkeypatch.setattr(probe, "read_login_state", lambda: state["login"])
    monkeypatch.setattr(probe, "probe_c2c_entrance", lambda: {})
    monkeypatch.setattr(T, "raise_tunnel_alert", lambda obs: shown.append(T.classify_probe(obs)))
    probe.run_tick("unused", "unused", {})
    probe.run_tick("unused", "unused", {})
    assert shown == [T.CAUSE_NOT_LOGGED_IN]
    state["login"] = login.LOGIN_VALID
    probe.run_tick("unused", "unused", {})
    state["login"] = login.LOGIN_NONE
    probe.run_tick("unused", "unused", {})
    assert shown == [T.CAUSE_NOT_LOGGED_IN, T.CAUSE_NOT_LOGGED_IN]
