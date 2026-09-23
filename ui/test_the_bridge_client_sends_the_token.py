# -*- coding: utf-8 -*-
"""The chat window's bridge client, EXECUTED against a bridge -- not read as text.

ui/BridgeClient.cs is how CopilotChat reaches bridge/copilot_bridge.py since the bridge began
refusing unauthenticated and GET requests (SEC-04, 2026-09-24). This compiles the shipped file
with a test-only driver (ui/testdata/BridgeClientHarness.cs) using the real csc, and runs it:

* against a capturing server: the request is a POST, carries X-Bridge-Token equal to the token
  file, sends the query as the form body unchanged, and carries no Origin / Referer;
* against the real bridge Handler on a free port (page layer stubbed; never the live :8765):
  a /send gets through and lands in the store;
* across a bridge restart: the same process, token cached, gets through after the token
  rotates, because it re-reads the file on the 401;
* deploy order: no token file, and an old GET-only bridge, each give a message that says what
  to do; and an OLD window's bare GET is told, in the text it shows, to rebuild.

Skips only on a non-Windows host, or without csc unless REQUIRE_CSC=1 (CI sets it).
"""
from __future__ import annotations

import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.parse

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

UI = os.path.join(REPO, "ui")
FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")
SOURCES = [os.path.join(UI, "BridgeClient.cs"),
           os.path.join(UI, "testdata", "BridgeClientHarness.cs")]

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the client is .NET Framework C#")


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    if not os.path.isfile(CSC):
        if os.environ.get("REQUIRE_CSC", "").strip() == "1":
            pytest.fail("csc.exe is not at %s and REQUIRE_CSC=1" % CSC)
        pytest.skip("csc.exe is not at %s" % CSC)
    exe = str(tmp_path_factory.mktemp("bridgeclient") / "BridgeClientHarness.exe")
    r = childproc.run([CSC, "/nologo", "/target:exe", "/out:" + exe] + SOURCES, timeout=300)
    assert r.returncode == 0 and os.path.isfile(exe), (
        "csc could not build the bridge client (rc=%s):\n%s\n%s" % (r.returncode, r.stdout, r.stderr))
    return exe


def _run(exe, tmp_path, *args):
    out = str(tmp_path / ("out_%d.txt" % time.monotonic_ns()))
    r = childproc.run([exe, args[0], out] + list(args[1:]), timeout=120)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    with open(out, encoding="utf-8") as fh:
        return [ln for ln in fh.read().splitlines() if ln]


@pytest.fixture()
def token_dir(tmp_path, monkeypatch):
    from bridge import bridge_auth
    d = tmp_path / "token"
    monkeypatch.setenv(bridge_auth.TOKEN_DIR_ENV, str(d))   # inherited by the harness
    return d


def _serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


class _Capture(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("ascii") if n else ""
        type(self).seen.append({"method": self.command, "path": self.path,
                                "headers": {k.lower(): v for k, v in self.headers.items()},
                                "body": body})
        out = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _any


@pytest.fixture()
def real_bridge(tmp_path, monkeypatch, token_dir):
    """The real Handler on a free port, page layer stubbed so nothing can reach a browser."""
    monkeypatch.setenv("MCP_SESSION_STORE_DIR", str(tmp_path / "sessions"))
    from bridge import session_store as S
    monkeypatch.setattr(S, "SESS_DIR", str(tmp_path / "sessions"), raising=False)
    assert S._base_dir() == str(tmp_path / "sessions"), "the store is not isolated"
    import bridge.copilot_bridge as B
    from bridge import bridge_auth
    monkeypatch.setattr(B, "PAGE", None)       # whatever an earlier test left on the module
    monkeypatch.setattr(B, "DRIVER", None)

    def _no_page(fn, *a, **kw):
        raise AssertionError("page work attempted in a hermetic bridge")

    monkeypatch.setattr(B, "run_on_page_thread", _no_page)
    srv, base = _serve(B.Handler)
    token, _ = bridge_auth.install_token(srv.server_address[1])
    monkeypatch.setattr(B, "BRIDGE_TOKEN", token)
    try:
        yield base, B
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_request_is_a_post_carrying_the_token_and_the_form(harness, tmp_path, token_dir):
    from bridge import bridge_auth
    _Capture.seen = []
    srv, base = _serve(_Capture)
    try:
        token, _ = bridge_auth.install_token(srv.server_address[1])
        q = "/switch?url=" + urllib.parse.quote("https://x/a?b=c&d=e f", safe="") + "&n=1"
        assert _run(harness, tmp_path, "call", base, q) == ['OK {"ok": true}']
    finally:
        srv.shutdown()
        srv.server_close()
    assert len(_Capture.seen) == 1, _Capture.seen
    r = _Capture.seen[0]
    assert r["method"] == "POST" and r["path"] == "/switch", r
    assert r["headers"].get("x-bridge-token") == token, "the token was not sent"
    assert r["headers"].get("content-type") == "application/x-www-form-urlencoded"
    assert r["body"] == q.split("?", 1)[1], "the form was changed on the way"
    for h in ("origin", "referer", "sec-fetch-site"):
        assert h not in r["headers"], h
    assert "expect" not in r["headers"], "100-continue stalls every call against HTTP/1.0"


def test_a_send_gets_through_the_real_bridge(harness, tmp_path, real_bridge):
    base, B = real_bridge
    from bridge import session_store as S
    msg = "from the window & 100% + \u65e5\u672c\u8a9e"
    got = _run(harness, tmp_path, "call", base,
               "/send?msg=" + urllib.parse.quote(msg, safe=""))
    assert got[0].startswith("OK "), got
    sid = json.loads(got[0][3:])["sid"]
    assert (S.load(sid) or {}).get("pending")[-1] == msg


def test_a_restarted_bridge_does_not_strand_a_running_window(harness, tmp_path, real_bridge):
    """One process, token cached after the first call; the bridge restarts (new token); the
    second call gets a 401, re-reads the file, and goes through."""
    base, B = real_bridge
    from bridge import bridge_auth
    go = str(tmp_path / "go")
    out_first = None
    proc_out = str(tmp_path / "twice.txt")
    import subprocess
    p = subprocess.Popen([harness, "twice", proc_out, base, "/send?msg=one", "/send?msg=two", go],
                         creationflags=childproc.headless_creationflags())
    try:
        for _ in range(300):
            if os.path.exists(proc_out + ".first"):
                break
            time.sleep(0.1)
        with open(proc_out + ".first", encoding="utf-8") as fh:
            out_first = fh.read()
        assert out_first.startswith("OK "), out_first
        new, _ = bridge_auth.install_token(bridge_auth.port_of(base))     # the restart
        B.BRIDGE_TOKEN = new
        open(go, "w").close()
        assert p.wait(timeout=60) == 0
    finally:
        if p.poll() is None:
            p.kill()
    with open(proc_out, encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln]
    assert len(lines) == 2 and lines[1].startswith("OK "), lines


def test_no_token_file_says_what_to_do(harness, tmp_path, real_bridge, monkeypatch):
    base, _ = real_bridge
    from bridge import bridge_auth
    monkeypatch.setenv(bridge_auth.TOKEN_DIR_ENV, str(tmp_path / "nowhere"))
    got = _run(harness, tmp_path, "call", base, "/send?msg=x")
    assert got[0].startswith("ERR 0 No bridge token at"), got
    assert "older than this window" in got[0] and "Restart the bridge" in got[0]


def test_an_old_get_only_bridge_says_what_to_do(harness, tmp_path, token_dir):
    from bridge import bridge_auth

    class _Old(http.server.BaseHTTPRequestHandler):
        """Answers POST with 501 (or a reset: it never reads the body); /status as the old
        bridge did, with no "authenticated" field."""
        def log_message(self, *a):
            pass

        def do_GET(self):
            out = b'{"ok": true, "transport": "page", "busy": false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv, base = _serve(_Old)
    try:
        bridge_auth.install_token(srv.server_address[1])
        got = _run(harness, tmp_path, "call", base, "/send?msg=x")
    finally:
        srv.shutdown()
        srv.server_close()
    assert got[0].startswith("ERR 501 "), got
    assert "older than this window" in got[0]


def test_an_old_window_is_told_to_rebuild_in_the_text_it_shows(harness, tmp_path, real_bridge):
    """The old CopilotChat streamed with a bare GET and, on an error status, showed .NET's
    WebException message -- which on this machine is localized and DROPS the server's reason
    phrase (measured: "(405) メソッドは使用できません"). So the bridge answers that GET with a
    one-frame stream whose text is the instruction; nothing is run."""
    base, B = real_bridge
    got = _run(harness, tmp_path, "legacyget", base, "/stream?msg=hi")
    assert got[0].startswith("OK "), got
    assert "[bridge error:" in got[0] and "rebuild_ui.ps1" in got[0], got
    # and what the old window's own parser would take out of it (ExtractField "replace")
    frame = [ln for ln in got[0][3:].split("\n") if ln.startswith("data:")][0]
    assert json.loads(frame[5:])["replace"] == B.Handler.OUTDATED_CLIENT_TEXT


def test_every_bridge_call_in_the_window_goes_through_the_client():
    """The only raw request left in CopilotChat.cs is the reachability probe of the token-free
    /conv. Any other `_bridge + ...` request would be a bridge call without the token."""
    with open(os.path.join(UI, "CopilotChat.cs"), encoding="utf-8-sig") as fh:
        src = fh.read()
    raw = re.findall(r"WebRequest\.Create\(\s*_bridge\s*\+\s*([^)]*)\)", src)
    assert raw == ['"/conv"'], raw
    assert "BridgeClient.Call(_bridge" in src and "BridgeClient.Open(_bridge" in src
