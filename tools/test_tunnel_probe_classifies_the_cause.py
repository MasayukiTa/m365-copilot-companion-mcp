"""The always-on public-path probe must name WHICH cause failed, not just 'down'.

These cover tools/tunnel_probe.py -- the pure decision core the PowerShell watcher
(scripts/public_url_probe.ps1) calls after it measures. The point the task makes is that
an alert that says 'public path down' when the real cause is 'not logged in' sends the
operator to the wrong fix, so each cause gets its own asserted verdict, and the ORDER
(most-upstream cause wins) is pinned so a login failure is never reported as a relay
failure.

Hermetic: no network, no subprocess, no Windows. Every input is a literal.
"""

from __future__ import annotations

import json

from tools.tunnel_probe import (
    LOCAL_SERVER_DOWN,
    NO_HOST,
    NOT_LOGGED_IN,
    OK,
    PUBLIC_PATH_DOWN,
    Observation,
    classify,
    is_healthy_mainpy_body,
    is_logged_in,
)


# --- is_healthy_mainpy_body: the same body rule as the supervisor ------------------------

def test_healthy_body_is_status_ok_and_positive_pid():
    assert is_healthy_mainpy_body(json.dumps({"status": "ok", "server_pid": 1234})) is True


def test_status_ok_but_pid_zero_is_not_healthy():
    assert is_healthy_mainpy_body(json.dumps({"status": "ok", "server_pid": 0})) is False


def test_status_ok_but_no_pid_is_not_healthy():
    assert is_healthy_mainpy_body(json.dumps({"status": "ok"})) is False


def test_a_sign_in_page_html_is_not_healthy():
    # Exactly the relay anti-phishing / sign-in false positive the probe must reject.
    assert is_healthy_mainpy_body("<html><body>Sign in to continue</body></html>") is False


def test_empty_or_none_body_is_not_healthy():
    assert is_healthy_mainpy_body("") is False
    assert is_healthy_mainpy_body(None) is False


def test_non_json_200_body_is_not_healthy():
    assert is_healthy_mainpy_body("OK") is False


def test_pid_as_numeric_string_still_counts():
    # main.py serialises server_pid as an int, but a numeric string must not be rejected
    # on a technicality -- int("1234") > 0 is the real question.
    assert is_healthy_mainpy_body(json.dumps({"status": "ok", "server_pid": "1234"})) is True


# --- is_logged_in: devtunnel user show parsing -------------------------------------------

def test_not_logged_in_phrase_is_signed_out():
    assert is_logged_in("Not logged in") is False
    assert is_logged_in("Not logged in.") is False
    assert is_logged_in("NOT LOGGED IN") is False


def test_empty_user_show_is_signed_out():
    assert is_logged_in("") is False
    assert is_logged_in("   ") is False
    assert is_logged_in(None) is False


def test_an_account_line_is_signed_in():
    assert is_logged_in("Logged in as user@contoso.com using Microsoft account") is True
    assert is_logged_in("user@contoso.com (Microsoft)") is True


# --- classify: each cause, named, in priority order --------------------------------------

def _obs(user="user@contoso.com", host=1, local=True, public=True):
    return Observation(user_show_text=user, host_connections=host,
                       local_ok=local, public_ok=public)


def test_all_good_is_ok():
    v = classify(_obs())
    assert v.code == OK
    assert v.healthy is True


def test_not_logged_in_wins_over_everything():
    # Even if local, host and public would otherwise look fine, a signed-out CLI is cause #1.
    v = classify(_obs(user="Not logged in", host=0, local=False, public=False))
    assert v.code == NOT_LOGGED_IN
    assert v.healthy is False


def test_local_server_down_is_reported_before_public():
    # Signed in, but the local server is not main.py -> do NOT blame the public path.
    v = classify(_obs(user="user@contoso.com", host=1, local=False, public=False))
    assert v.code == LOCAL_SERVER_DOWN


def test_no_host_when_signed_in_local_up_and_zero_connections():
    v = classify(_obs(user="user@contoso.com", host=0, local=True, public=False))
    assert v.code == NO_HOST


def test_public_path_down_when_everything_upstream_is_fine():
    # Signed in, local up, a host exists, but the public body is wrong (relay 404/504 etc).
    v = classify(_obs(user="user@contoso.com", host=1, local=True, public=False))
    assert v.code == PUBLIC_PATH_DOWN


def test_a_reachable_public_body_proves_a_host_even_if_count_unknown():
    # host_connections None (CLI unreadable) but the public body is healthy -> OK, not no_host.
    v = classify(_obs(user="user@contoso.com", host=None, local=True, public=True))
    assert v.code == OK


def test_unknown_host_count_with_dead_public_is_not_misread_as_ok():
    # host None AND public not ok: the public path is down; we must not silently pass.
    v = classify(_obs(user="user@contoso.com", host=None, local=True, public=False))
    assert v.code == PUBLIC_PATH_DOWN


def test_every_failure_verdict_carries_a_detail_clause():
    for v in (
        classify(_obs(user="Not logged in")),
        classify(_obs(local=False)),
        classify(_obs(host=0, public=False)),
        classify(_obs(public=False)),
    ):
        assert v.healthy is False
        assert v.detail and isinstance(v.detail, str)
