# -*- coding: utf-8 -*-
r"""doctor.ps1 tunnel_serving under a TENANT-ONLY grant (colleague's PC, 2026-10-20).

THE FALSE FAIL. With the grant 'tenant only' a healthy tunnel answers an ANONYMOUS caller with a
redirect to login.microsoftonline.com. The probe (Test-TunnelHealthAnswer) is anonymous, so it
printed "[FAIL] Dev Tunnel host serving ... redirected to login.microsoftonline.com" and advised
start_all.bat, while "Running supervisor hosts the tunnel" and "Last background start OK" were
green. A FAIL also sets tunnelChainBroken, which skips the checks after it.

THE RULE. Grant read back = 'tenant' AND the probe's only complaint is the redirect to a Microsoft
sign-in host: hosting is judged from `devtunnel show` "Host connections" >= 1 (the supervisor's own
parse) AND this machine's server answering /health. 0 or unreadable stays FAIL. An anonymous grant
and another host's pid are unchanged.

HOW IT RUNS. The doctor block itself (3c.1 through "# END tunnel_serving") is extracted from the
live file and run in Windows PowerShell against the stub devtunnel of
test_setup_devtunnel_access_and_identity.py (listing wording measured on devtunnel 1.0.1516), a
stub public URL on an ephemeral 127.0.0.1 port and a stub local /health on another one. Nothing
touches port 8000, the real tunnel or the real .fleet.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_setup_devtunnel_access_and_identity as base  # noqa: E402
from test_setup_devtunnel_access_and_identity import DOCTOR_PS1, Rig, _extract, _run_ps  # noqa: E402

pytestmark = base.pytestmark

TENANT_LISTING = " 0: +AAD Tenant [connect] (9d07533c-a4e0-4639-9782-aa2cae12a928)"
ANON_LISTING = "+Anonymous [connect]"
UTIL_PS1 = os.path.join(HERE, "tunnel_name_util.ps1")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        mode = self.server.mode
        redirects = {
            "signin": "https://login.microsoftonline.com/common/oauth2/authorize",
            "relay-signin": "https://global.rel.tunnels.api.visualstudio.com/api/v1/auth/login",
            "lookalike": "https://evil-tunnels.api.visualstudio.com.example.com/login",
            "lookalike2": "https://evil-tunnels.api.visualstudio.com/login",
        }
        if mode in redirects:
            self.send_response(302)
            self.send_header("Location", redirects[mode])
            self.end_headers()
            return
        pid = 999 if mode == "other-pid" else 4242
        body = ('{"status": "ok", "server_pid": %d}' % pid).encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _server(mode):
    s = HTTPServer(("127.0.0.1", 0), _Handler)
    s.mode = mode
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_doctor_block(tmp_path, listing, hosts, public_mode, local_up=True):
    src = open(DOCTOR_PS1, encoding="utf-8").read()
    block = src[src.index("# 3c.1 The tunnel's ACCESS GRANT"):src.index("# END tunnel_serving")]
    pieces = [_extract(src, "function " + f) for f in
              ("Add-Result", "Invoke-DevTunnelBounded", "TunnelCheck")]
    tun = {"tunnel": [listing], "ports": {"8000": []}}
    if hosts is not None:
        tun["hosts"] = hosts
    rig = Rig(tmp_path, "", {"owned": {"tun": tun}})
    public = _server(public_mode)
    local = _server("json") if local_up else None
    local_url = ("http://127.0.0.1:%d/health" % local.server_port) if local else (
        "http://127.0.0.1:%d/health" % _closed_port())
    body = (". '%s'\n" % UTIL_PS1 + "\n".join(pieces) + "\n"
            "$script:results = @(); $script:ok = 0; $script:bad = 0; $script:warn = 0;"
            " $script:unknown = 0\n$script:tunnelChainBroken = $false\n$repo = '%s'\n"
            "$tname = 'tun'\n$turl = 'http://127.0.0.1:%d/mcp'\n$localHealthUrl = '%s'\n"
            "$DevTunnel = '%s'\n" % (tmp_path, public.server_port, local_url,
                                    rig.stub / "devtunnel.cmd")
            + block +
            "\n$r = @($script:results | Where-Object { $_.id -eq 'tunnel_serving' })[0]\n"
            "Write-Output ('RESULT|' + $r.ok + '|' + $r.skipped + '|' + $script:bad + '|' + $r.fix)\n")
    try:
        out = _run_ps(tmp_path, body, dict(rig.env, NO_PROXY="127.0.0.1,localhost"))
    finally:
        public.shutdown()
        if local:
            local.shutdown()
    row = [l for l in out.splitlines() if l.startswith("RESULT|")][-1].split("|", 4)
    return out, row


def test_tenant_grant_with_a_hosted_tunnel_passes_without_probing_the_sign_in_page(tmp_path):
    out, row = _run_doctor_block(tmp_path, TENANT_LISTING, 1, "signin")
    assert "[ OK ] Dev Tunnel host serving" in out, out
    assert row[1] == "True", out
    assert "[FAIL]" not in out, out
    shown = "\n".join(l for l in out.splitlines() if not l.startswith("RESULT|"))
    assert "start_all.bat" not in shown, out   # the stored fix text is not printed on a pass
    assert "cannot be probed anonymously" in out and "host connection count (1)" in out, out


def test_relay_sign_in_host_passes_under_tenant_with_a_host_connection(tmp_path):
    out, row = _run_doctor_block(tmp_path, TENANT_LISTING, 1, "relay-signin")
    assert "[ OK ] Dev Tunnel host serving" in out, out
    assert row[1] == "True", out
    assert "[FAIL]" not in out, out
    assert "host connection count (1)" in out, out


def test_relay_sign_in_host_with_zero_host_connections_stays_a_fail(tmp_path):
    out, row = _run_doctor_block(tmp_path, TENANT_LISTING, 0, "relay-signin")
    assert "[FAIL] Dev Tunnel host serving" in out, out
    assert row[1] == "False", out
    assert "0 host connections" in row[4], row


@pytest.mark.parametrize("mode", ["lookalike", "lookalike2"])
def test_a_lookalike_relay_host_stays_a_fail_under_tenant(tmp_path, mode):
    out, row = _run_doctor_block(tmp_path, TENANT_LISTING, 1, mode)
    assert "[FAIL] Dev Tunnel host serving" in out, out
    assert row[1] == "False", out
    assert "host connection count" not in out, out


def test_anonymous_grant_with_the_relay_redirect_stays_a_fail(tmp_path):
    out, row = _run_doctor_block(tmp_path, ANON_LISTING, 1, "relay-signin")
    assert "[FAIL] Dev Tunnel host serving" in out, out
    assert row[1] == "False", out
    assert "redirected to global.rel.tunnels.api.visualstudio.com" in row[4], row
    assert "host connection count" not in out, out


def test_tenant_grant_with_zero_host_connections_stays_a_fail(tmp_path):
    out, row = _run_doctor_block(tmp_path, TENANT_LISTING, 0, "signin")
    assert "[FAIL] Dev Tunnel host serving" in out, out
    assert row[1] == "False", out
    assert "0 host connections" in row[4] and "start_all.bat" in row[4], row


def test_tenant_grant_with_an_unreadable_count_stays_a_fail(tmp_path):
    out, row = _run_doctor_block(tmp_path, TENANT_LISTING, None, "signin")
    assert "[FAIL] Dev Tunnel host serving" in out, out
    assert "no readable count" in row[4], row


def test_tenant_grant_with_a_hosted_tunnel_but_no_local_server_stays_a_fail(tmp_path):
    out, row = _run_doctor_block(tmp_path, TENANT_LISTING, 1, "signin", local_up=False)
    assert "[FAIL] Dev Tunnel host serving" in out, out
    assert "does not answer" in row[4], row


def test_anonymous_grant_with_the_same_redirect_stays_a_fail(tmp_path):
    out, row = _run_doctor_block(tmp_path, ANON_LISTING, 1, "signin")
    assert "[FAIL] Dev Tunnel host serving" in out, out
    assert row[1] == "False", out
    assert "redirected to login.microsoftonline.com" in row[4], row
    assert "host connection count" not in out, out


def test_another_pcs_pid_is_still_a_fail_under_a_tenant_grant(tmp_path):
    out, row = _run_doctor_block(tmp_path, TENANT_LISTING, 1, "other-pid")
    assert "[FAIL] Dev Tunnel host serving" in out, out
    assert "another machine also hosts this tunnel" in row[4], row


def test_the_host_connection_count_is_parsed_like_the_supervisors(tmp_path):
    """One regex, two readers: the shared helper and supervisor.ps1's own line agree."""
    sup = open(os.path.join(HERE, "supervisor.ps1"), encoding="utf-8").read()
    assert "if ($out -match 'Host connections\\s*:\\s*(\\d+)') { return [int]$Matches[1] }" in sup
    util = open(UTIL_PS1, encoding="utf-8").read()
    fn = _extract(util, "function Get-HostConnectionsFromShow")
    samples = ["Tunnel ID : a.jpe1\nHost connections      : 1\nPorts : 1",
               "Tunnel ID : a.jpe1\n  Host connections : 0\n",
               "Tunnel ID : a.jpe1\nPorts : 1\n",
               "Tunnel not found."]
    body = fn + "\n"
    for i, s in enumerate(samples):
        p = tmp_path / ("s%d.txt" % i)
        p.write_text(s, encoding="ascii")
        body += ("$t = Get-Content -Raw '%s'\n"
                 "$a = Get-HostConnectionsFromShow @($t -split \"`r?`n\")\n"
                 "if ($t -match 'Host connections\\s*:\\s*(\\d+)') { $b = [int]$Matches[1] } else { $b = $null }\n"
                 "Write-Output ('' + $a + '|' + $b)\n" % p)
    got = _run_ps(tmp_path, body).split()
    assert got == ["1|1", "0|0", "|", "|"], got
