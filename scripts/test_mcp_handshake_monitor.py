# -*- coding: utf-8 -*-
r"""The synthetic MCP-handshake monitor must call a 404 (or a ~60 s wait) a failure.

WHY THESE TESTS EXIST (measured 2026-11-11)
-------------------------------------------
The owner's chat client 'muse' saw the MCP ``initialize`` handshake fail with HTTP 404
after ~60 s while our old monitoring only did ``GET /health`` and saw 200 everywhere. The
pure decision core (scripts/mcp_handshake_monitor.py) is what turns measured facts into the
words the alert and the supervisor act on. These tests pin the four things the GOAL asks
for, against stub inputs -- no network, no clock, no process calls, so they run on the
Linux CI runner:

  1. a 404 is a failure; a response slower than the ceiling is a failure (not only non-200);
  2. an alert is raised only after 2 consecutive failures;
  3. a PUBLIC 404 while devtunnel host connections = 0 is named as such (the hypothesis);
  4. the gateway-down verdict follows the PUBLIC handshake, not the loopback /health.

The CI requirement "a fake server that returns 404 after 60 s" is expressed here as the
exact facts such a server produces: initialize_status=404, initialize_latency_s~=60. The
pure core never waits; it judges the recorded status and latency. A live 60 s fake server
is a non-hermetic integration check and is intentionally NOT on the CI runner.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mcp_handshake_monitor as core  # noqa: E402


# -- 1. classify_handshake: 404 and slowness are failures -----------------------------------

def test_fast_200_passes():
    r = core.classify_handshake(200, 1.2)
    assert r["ok"] is True and r["reason"] == "ok"


def test_404_is_a_failure():
    r = core.classify_handshake(404, 0.3)
    assert r["ok"] is False and r["reason"] == "not_found_404"


def test_a_fake_server_that_returns_404_after_60s_is_a_failure():
    # The exact facts a "404 after 60 s" fake server produces.
    r = core.classify_handshake(404, 60.0)
    assert r["ok"] is False and r["reason"] == "not_found_404"


def test_slow_200_is_a_failure_even_though_it_answered_200():
    r = core.classify_handshake(200, 45.0)
    assert r["ok"] is False and r["reason"] == "slow"


def test_the_slow_ceiling_boundary():
    assert core.classify_handshake(200, core.SLOW_SECONDS)["ok"] is True      # == ceiling is ok
    assert core.classify_handshake(200, core.SLOW_SECONDS + 0.01)["ok"] is False


def test_no_http_answer_is_a_failure():
    assert core.classify_handshake("ERR", 75.0)["ok"] is False
    assert core.classify_handshake(None, 75.0)["reason"] == "no_answer"


def test_auth_rejection_is_a_failure_named_auth():
    assert core.classify_handshake(401, 0.2)["reason"] == "auth"
    assert core.classify_handshake(403, 0.2)["reason"] == "auth"


def test_other_non_200_carries_its_status():
    assert core.classify_handshake(502, 0.2)["reason"] == "http_502"


def test_a_200_with_unreadable_latency_is_not_turned_into_a_failure():
    r = core.classify_handshake(200, None)
    assert r["ok"] is True and r["reason"] == "ok_latency_unknown"


# -- 2. hop classification -----------------------------------------------------------------

def test_local_failure_is_blamed_on_the_local_server():
    res = core.classify_handshake("ERR", 5.0)
    assert core.classify_hop("local", res) == core.HOP_LOCAL_SERVER


def test_public_404_with_zero_host_connections_is_the_zero_conn_hop():
    res = core.classify_handshake(404, 60.0)
    assert core.classify_hop("public", res, host_connections=0) == core.HOP_HOST_ZERO_CONN


def test_public_404_with_a_live_host_connection_is_blamed_on_the_relay():
    res = core.classify_handshake(404, 60.0)
    assert core.classify_hop("public", res, host_connections=1) == core.HOP_DEVTUNNEL_RELAY


def test_public_404_with_an_unreadable_host_count_is_not_blamed_on_zero():
    res = core.classify_handshake(404, 60.0)
    assert core.classify_hop("public", res, host_connections=None) == core.HOP_DEVTUNNEL_RELAY


def test_auth_hop_wins_over_scope():
    res = core.classify_handshake(401, 0.2)
    assert core.classify_hop("public", res, host_connections=0) == core.HOP_AUTH


def test_a_passing_result_has_no_failing_hop():
    res = core.classify_handshake(200, 1.0)
    assert core.classify_hop("public", res, host_connections=5) == core.HOP_NONE


# -- 3. consecutive failures and the 2-strike alert -----------------------------------------

def test_a_single_failure_does_not_alert():
    fail = core.classify_handshake(404, 1.0)
    n = core.update_consecutive_failures(0, fail)
    assert n == 1
    assert core.decide_alert(n) is False


def test_two_consecutive_failures_raise_the_alert():
    fail = core.classify_handshake(404, 1.0)
    n = core.update_consecutive_failures(0, fail)
    n = core.update_consecutive_failures(n, fail)
    assert n == 2
    assert core.decide_alert(n) is True


def test_a_pass_resets_the_streak():
    fail = core.classify_handshake(404, 1.0)
    ok = core.classify_handshake(200, 1.0)
    n = core.update_consecutive_failures(0, fail)
    n = core.update_consecutive_failures(n, ok)
    assert n == 0
    assert core.decide_alert(n) is False


def test_a_corrupt_saved_counter_cannot_inflate_into_an_alert():
    fail = core.classify_handshake(404, 1.0)
    # A garbage previous count reads as 0, so one failure is 1, not an instant alert.
    n = core.update_consecutive_failures("garbage", fail)
    assert n == 1 and core.decide_alert(n) is False


def test_an_unreadable_streak_never_alerts():
    assert core.decide_alert(None) is False
    assert core.decide_alert("x") is False


# -- 3b. the 404-coincides-with-zero-host-connections sentence -------------------------------

def test_404_with_zero_host_connections_correlates():
    res = core.classify_handshake(404, 60.0)
    assert core.correlates_404_with_zero_host_connections(res, 0) is True


def test_404_with_a_live_host_connection_does_not_correlate():
    res = core.classify_handshake(404, 60.0)
    assert core.correlates_404_with_zero_host_connections(res, 2) is False


def test_404_with_an_unreadable_host_count_does_not_assert_a_correlation():
    res = core.classify_handshake(404, 60.0)
    assert core.correlates_404_with_zero_host_connections(res, None) is False


def test_a_non_404_failure_does_not_correlate_even_at_zero_hosts():
    res = core.classify_handshake("ERR", 60.0)
    assert core.correlates_404_with_zero_host_connections(res, 0) is False


# -- 4. the gateway-down verdict follows the PUBLIC handshake, not /health -------------------

def test_gateway_is_down_when_public_fails_even_if_local_is_green():
    local_ok = core.classify_handshake(200, 1.0)
    public_404 = core.classify_handshake(404, 60.0)
    v = core.gateway_is_down(local_ok, public_404)
    assert v["gateway_down"] is True
    assert v["local_ok"] is True and v["public_ok"] is False
    assert v["scope"] == "public_only"


def test_gateway_is_up_only_when_the_public_handshake_passes():
    local_ok = core.classify_handshake(200, 1.0)
    public_ok = core.classify_handshake(200, 1.2)
    v = core.gateway_is_down(local_ok, public_ok)
    assert v["gateway_down"] is False and v["scope"] == "none"


def test_both_legs_down_is_scope_both():
    local_bad = core.classify_handshake("ERR", 5.0)
    public_bad = core.classify_handshake(404, 60.0)
    v = core.gateway_is_down(local_bad, public_bad)
    assert v["gateway_down"] is True and v["scope"] == "both"


def test_local_down_but_public_up_is_local_only_and_gateway_is_not_down():
    local_bad = core.classify_handshake("ERR", 5.0)
    public_ok = core.classify_handshake(200, 1.0)
    v = core.gateway_is_down(local_bad, public_ok)
    assert v["gateway_down"] is False and v["scope"] == "local_only"


# -- 4b. the supervisor reads its gateway verdict from THIS monitor, not from /health -------

def _status(public_result, consecutive_failures, url_configured=True):
    return {
        "public": {
            "url_configured": url_configured,
            "result": public_result,
            "consecutive_failures": consecutive_failures,
        },
    }


def test_supervisor_sees_down_after_two_consecutive_public_failures_in_a_fresh_status():
    fail = core.classify_handshake(404, 60.0)
    st = _status(fail, 2)
    v = core.decide_gateway_from_monitor(st, status_age_seconds=20)
    assert v["verdict"] == "down"


def test_supervisor_does_not_see_down_on_a_single_failing_tick():
    fail = core.classify_handshake(404, 60.0)
    st = _status(fail, 1)
    v = core.decide_gateway_from_monitor(st, status_age_seconds=20)
    assert v["verdict"] == "up" and "below_threshold" in v["reason"]


def test_supervisor_sees_up_when_the_public_leg_passes():
    ok = core.classify_handshake(200, 1.2)
    st = _status(ok, 0)
    v = core.decide_gateway_from_monitor(st, status_age_seconds=20)
    assert v["verdict"] == "up"


def test_a_stale_status_is_never_read_as_gateway_down():
    # The monitor stopped: even a 2-strike failure in an OLD file must not declare down.
    fail = core.classify_handshake(404, 60.0)
    st = _status(fail, 5)
    v = core.decide_gateway_from_monitor(st, status_age_seconds=10_000)
    assert v["verdict"] == "stale"


def test_a_missing_or_unparseable_status_is_unknown_not_down():
    assert core.decide_gateway_from_monitor(None, status_age_seconds=1)["verdict"] == "unknown"
    assert core.decide_gateway_from_monitor("not a dict", status_age_seconds=1)["verdict"] == "unknown"


def test_an_unknown_age_is_unknown_not_down():
    fail = core.classify_handshake(404, 60.0)
    st = _status(fail, 9)
    assert core.decide_gateway_from_monitor(st, status_age_seconds=None)["verdict"] == "unknown"


def test_no_public_target_configured_is_unknown():
    fail = core.classify_handshake(404, 60.0)
    st = _status(fail, 9, url_configured=False)
    assert core.decide_gateway_from_monitor(st, status_age_seconds=20)["verdict"] == "unknown"
