"""Unit tests for the layered provider probe. No network: classify is pure, and the
probe's layer order is checked with a patched socket layer."""

from __future__ import annotations

import json
import socket
import ssl

import pytest

from tools import provider_layer_probe as lp


def row(host, role, layer=None):
    return {"host": host, "role": role, "fail_layer": layer}


def test_all_layers_pass_is_ok():
    rows = [row("c", "control"), row("p", "provider"), row("s", "signin")]
    assert lp.classify(rows) == "ok"


def test_control_failure_means_our_network():
    rows = [row("c", "control", "tcp"), row("p", "provider", "tcp")]
    assert lp.classify(rows) == "control_down"


def test_control_failure_wins_over_tls_failure_on_provider():
    rows = [row("c", "control", "dns"), row("p", "provider", "tls")]
    assert lp.classify(rows) == "control_down"


def test_provider_tcp_failure_with_control_ok_is_unreachable():
    rows = [row("c", "control"), row("p", "provider", "tcp")]
    assert lp.classify(rows) == "provider_unreachable"


def test_provider_dns_failure_with_control_ok_is_unreachable():
    rows = [row("c", "control"), row("p", "provider", "dns")]
    assert lp.classify(rows) == "provider_unreachable"


@pytest.mark.parametrize("layer", ["tls", "http"])
def test_provider_tls_or_http_failure_is_unresponsive(layer):
    rows = [row("c", "control"), row("p", "provider", layer)]
    assert lp.classify(rows) == "provider_unresponsive"


def test_agent_page_host_counts_as_provider():
    rows = [row("c", "control"), row("a", "agent", "tcp")]
    assert lp.classify(rows) == "provider_unreachable"


def test_no_rows_is_its_own_verdict():
    assert lp.classify([]) == "no_rows"


def test_unreachable_wins_over_unresponsive_when_both_fail():
    rows = [row("c", "control"), row("p1", "provider", "http"), row("p2", "provider", "tcp")]
    assert lp.classify(rows) == "provider_unreachable"


def test_exit_codes_are_distinct_per_verdict():
    codes = [lp.EXIT_CODES[v] for v in ("ok", "control_down", "provider_unreachable",
                                        "provider_unresponsive", "no_rows")]
    assert len(set(codes)) == len(codes)
    assert lp.EXIT_CODES["ok"] == 0


class _FakeTls:
    def __init__(self, sock, fail_on=None):
        self.sock = sock
        self.fail_on = fail_on
        self.closed = False

    def settimeout(self, _t):
        pass

    def sendall(self, _data):
        if self.fail_on == "http":
            raise TimeoutError("timed out")

    def recv(self, _n):
        return b"HTTP/1.1 200 OK\r\n"

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on

    def wrap_socket(self, sock, server_hostname=None):
        if self.fail_on == "tls":
            raise ssl.SSLError("handshake failed")
        return _FakeTls(sock, self.fail_on)


class _FakeSock:
    def close(self):
        pass


def _patch(monkeypatch, dns_fail=False, tcp_fail=False, tls_fail=None, http_fail=None):
    def getaddrinfo(host, port, *a, **k):
        if dns_fail:
            raise socket.gaierror("no such host")
        return [(None, None, None, None, ("192.0.2.1", 443))]

    def create_connection(addr, timeout=None):
        if tcp_fail:
            raise ConnectionRefusedError("refused")
        return _FakeSock()

    def create_default_context():
        return _FakeContext(tls_fail or http_fail)

    monkeypatch.setattr(lp.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(lp.socket, "create_connection", create_connection)
    monkeypatch.setattr(lp.ssl, "create_default_context", create_default_context)


def test_probe_host_records_dns_failure(monkeypatch):
    _patch(monkeypatch, dns_fail=True)
    r = lp.probe_host("p.example", "provider")
    assert r["fail_layer"] == "dns" and r["error"] == "gaierror"


def test_probe_host_records_tcp_failure(monkeypatch):
    _patch(monkeypatch, tcp_fail=True)
    r = lp.probe_host("p.example", "provider")
    assert r["fail_layer"] == "tcp" and r["error"] == "ConnectionRefusedError"


def test_probe_host_records_tls_failure(monkeypatch):
    _patch(monkeypatch, tls_fail="tls")
    r = lp.probe_host("p.example", "provider")
    assert r["fail_layer"] == "tls" and r["error"] == "SSLError"


def test_probe_host_records_http_failure(monkeypatch):
    _patch(monkeypatch, http_fail="http")
    r = lp.probe_host("p.example", "provider")
    assert r["fail_layer"] == "http" and r["error"] == "TimeoutError"


def test_probe_host_all_layers_pass(monkeypatch):
    _patch(monkeypatch)
    r = lp.probe_host("p.example", "provider")
    assert r["fail_layer"] is None and r["http_status"] == "200"


def test_default_provider_host_reads_ws_base(tmp_path, monkeypatch):
    fake = tmp_path / "chathub.py"
    fake.write_text('WS_BASE = "wss://example.test/Chathub"\n', encoding="utf-8")
    monkeypatch.setattr(lp, "CHATHUB", fake)
    assert lp.default_provider_host() == "example.test"


def test_append_evidence_is_one_json_line_per_call(tmp_path):
    target = tmp_path / ".fleet" / "provider_health.jsonl"
    rows = [{"ts": "t", "host": "h", "role": "control", "fail_layer": None}]
    lp.append_evidence(rows, "ok", target)
    lp.append_evidence(rows, "control_down", target)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["verdict"] == "control_down"
