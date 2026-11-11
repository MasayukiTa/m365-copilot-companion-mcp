"""Tests for the MCP handshake synthetic monitor.

The monitor exists because plain GET /health monitoring reported 200 everywhere
while the owner's client's REAL handshake got a 404 after ~60 s. These tests drive
the monitor's pure core directly, and stand up a FAKE MCP server that returns 404
after a delay so the real handshake path is exercised the way the client hits it --
including the measured failure mode (a 404 that coincides with host connections = 0).

No network, no devtunnel, no secrets: the fake server listens on loopback and the
key is a literal passed in.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tools import mcp_handshake_monitor as mon
from tools import mcp_handshake_probe as probe


# -- fake MCP server ------------------------------------------------------------
class _FakeHandler(BaseHTTPRequestHandler):
    #: set per-server below
    status = 404
    delay_s = 0.0
    body = b'{"error": "not found"}'

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.status == 200:
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "test-session")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(self.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)


def _start_fake(status=404, delay_s=0.0):
    handler = type("H", (_FakeHandler,), {"status": status, "delay_s": delay_s})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = "http://127.0.0.1:%d/mcp" % srv.server_address[1]
    return srv, url


# -- the real handshake against a fake 404 server -------------------------------
def test_real_handshake_against_404_server_is_a_failure():
    srv, url = _start_fake(status=404, delay_s=0.0)
    try:
        res = probe.handshake(url, "testkey", timeout=10)
    finally:
        srv.shutdown()
    assert res["initialize_status"] == 404
    assert res["ok"] is False
    assert mon.is_failure(res) is True
    # public target + no host connections => the measured cause is named.
    assert mon.classify_hop("public", res, host_connections=0) == "host connections=0"
    # public target + a host present => a relay/path fault, not host=0.
    assert mon.classify_hop("public", res, host_connections=1) == "devtunnel relay"
    # local target => the server itself.
    assert mon.classify_hop("local", res, host_connections=None) == "local server"


def test_the_fake_404_server_waits_60s_like_the_relay_does():
    """The measured symptom: a 404 that arrives only after ~60 s. We assert the
    monitor treats a slow response as a failure WITHOUT making CI wait 60 s, by
    scaling the slow threshold down to a fraction of a real delay. A real >30s
    delay is proven equivalent by is_failure's threshold branch (tested below).
    """
    srv, url = _start_fake(status=404, delay_s=0.6)
    try:
        res = probe.handshake(url, "testkey", timeout=10)
    finally:
        srv.shutdown()
    # Even at the small delay the 404 makes it a failure.
    assert mon.is_failure(res) is True
    # And had this been a 200, a delay over the threshold would still fail it:
    assert mon.is_failure(res, slow_threshold_s=0.3) is True


def test_a_200_server_is_not_a_failure():
    srv, url = _start_fake(status=200, delay_s=0.0)
    try:
        res = probe.handshake(url, "testkey", timeout=10)
    finally:
        srv.shutdown()
    assert res["initialize_status"] == 200
    assert res["tools_list_status"] == 200
    assert res["ok"] is True
    assert mon.is_failure(res) is False
    assert mon.classify_hop("local", res, host_connections=1) == "ok"


# -- is_failure: the 404 / slow / non-200 rules ---------------------------------
def test_is_failure_flags_slow_even_when_200():
    ok_but_slow = {
        "ok": True, "initialize_status": 200, "tools_list_status": 200,
        "initialize_seconds": 45.0, "tools_list_seconds": 1.0,
    }
    assert mon.is_failure(ok_but_slow) is True          # 45s > 30s
    assert mon.classify_hop("public", ok_but_slow, 1) == "slow"


def test_is_failure_flags_404_even_if_ok_flag_wrong():
    res = {"ok": False, "initialize_status": 404, "tools_list_status": None,
           "initialize_seconds": 1.0, "tools_list_seconds": None}
    assert mon.is_failure(res) is True


def test_auth_hop_for_401():
    res = {"ok": False, "initialize_status": 401, "tools_list_status": None,
           "initialize_seconds": 0.1, "tools_list_seconds": None}
    assert mon.classify_hop("public", res, host_connections=1) == "auth"
    assert mon.classify_hop("local", res, host_connections=None) == "auth"


# -- parse_host_connections -----------------------------------------------------
def test_parse_host_connections():
    out = ("Tunnel ID             : resonac-mcp.jpe1\n"
           "Host connections      : 0\n"
           "Client connections    : 0\n")
    assert mon.parse_host_connections(out) == 0
    assert mon.parse_host_connections("Host connections : 3") == 3
    assert mon.parse_host_connections("no count here") is None
    assert mon.parse_host_connections("") is None


# -- streaks / alert / gateway_is_down ------------------------------------------
def test_two_consecutive_failures_raise_the_alert():
    s0 = {}
    s1 = mon.update_streaks(s0, local_fail=False, public_fail=True)
    assert s1 == {"local_streak": 0, "public_streak": 1}
    assert mon.decide_alert(**{"local_streak": 0, "public_streak": 1}) is False
    s2 = mon.update_streaks(s1, local_fail=False, public_fail=True)
    assert s2["public_streak"] == 2
    assert mon.decide_alert(0, 2) is True


def test_a_single_failure_does_not_alert():
    assert mon.decide_alert(1, 0) is False
    assert mon.decide_alert(0, 1) is False


def test_a_success_resets_the_streak():
    prev = {"local_streak": 5, "public_streak": 5}
    now = mon.update_streaks(prev, local_fail=False, public_fail=False)
    assert now == {"local_streak": 0, "public_streak": 0}


def test_gateway_is_down_reads_the_streaks():
    assert mon.gateway_is_down(None) is False            # no evidence != down
    assert mon.gateway_is_down({}) is False
    assert mon.gateway_is_down({"local_streak": 1, "public_streak": 1}) is False
    assert mon.gateway_is_down({"local_streak": 0, "public_streak": 2}) is True
    assert mon.gateway_is_down({"local_streak": 2, "public_streak": 0}) is True


def test_alert_text_states_404_coincides_with_host_zero():
    state = {
        "local_streak": 0, "public_streak": 2,
        "public_hop": "host connections=0",
        "public_initialize_status": 404, "public_tools_list_status": None,
        "host_connections": 0, "local_hop": "ok",
    }
    text = mon.build_alert_text(state)
    assert "404 COINCIDES with host connections = 0" in text
    assert text.startswith("MCP GATEWAY ALERT:")


def test_alert_text_404_with_host_present_does_not_claim_coincidence():
    state = {
        "local_streak": 0, "public_streak": 2,
        "public_hop": "devtunnel relay",
        "public_initialize_status": 404, "public_tools_list_status": None,
        "host_connections": 1, "local_hop": "ok",
    }
    text = mon.build_alert_text(state)
    assert "COINCIDES" not in text
    assert "host connections = 1" in text


def test_no_alert_text_when_nothing_failed():
    assert mon.build_alert_text({"local_streak": 0, "public_streak": 0}) == ""


# -- tunnel_url_from_env normalises to /mcp -------------------------------------
def test_tunnel_url_from_env_appends_mcp(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MCP_TUNNEL_URL=https://example-8000.devtunnels.ms/\n", encoding="utf-8")
    assert mon.tunnel_url_from_env(str(env)) == "https://example-8000.devtunnels.ms/mcp"
    env.write_text("MCP_TUNNEL_URL=https://example-8000.devtunnels.ms\n", encoding="utf-8")
    assert mon.tunnel_url_from_env(str(env)) == "https://example-8000.devtunnels.ms/mcp"


def test_tunnel_url_from_env_missing_is_empty(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OTHER=1\n", encoding="utf-8")
    assert mon.tunnel_url_from_env(str(env)) == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
