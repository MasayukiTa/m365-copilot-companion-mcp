"""Bridge judge backend tests. Fast tests stub HTTP; live test is opt-in (MCP_BRIDGE_LIVE=1)."""
import json, os, sys, threading, http.server, importlib

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import tools.judge_backend as jb
import tools.command_judge as cj

_JUDGE_ENV = ("MCP_JUDGE_BACKEND", "MCP_BRIDGE_PORT")


@pytest.fixture(autouse=True)
def _restore_judge_env():
    """Put the judge selection back the way it was found.

    These tests set MCP_JUDGE_BACKEND=bridge in os.environ directly and reload
    tools.judge_backend so the module re-reads it. Without this, the setting outlives the
    file: tools/test_judge_backend.py runs next (b < j) and its
    test_the_default_is_no_judge -- whose entire subject is what get() returns when nothing
    is configured -- found "bridge" still configured and failed. It passed alone and failed
    in a run, which is the shape that costs the most to diagnose.

    The reload at the end matters as much as the environment: the module caches its choice
    at import, so restoring the variables without re-reading them leaves the stale one live.
    """
    saved = {k: os.environ.get(k) for k in _JUDGE_ENV}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(jb)

_OK = b'{"ok": true}'
_ANSWER = ('{"decision":"BLOCK_AND_RETRY","categories":["destructive"],"reason":"rm -rf of the home directory deletes all user files"}')

class _Stub(http.server.BaseHTTPRequestHandler):
    hits = []
    def log_message(self, *a):
        pass
    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        type(self).hits.append(path)
        if path in ("/new", "/status"):
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(_OK); return
        if path == "/stream":
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for ev in ({"delta": "thinking"}, {"replace": _ANSWER}, {}):
                self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode()); self.wfile.flush()
            return
        self.send_response(404); self.end_headers()

def _serve():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

def test_get_routes_bridge():
    os.environ["MCP_JUDGE_BACKEND"] = "bridge"; importlib.reload(jb)
    assert jb.get() is jb.bridge_judge

def test_fresh_conversation_before_turn_and_verdict_parses():
    _Stub.hits = []; srv = _serve()
    os.environ["MCP_BRIDGE_PORT"] = str(srv.server_address[1])
    os.environ["MCP_JUDGE_BACKEND"] = "bridge"; importlib.reload(jb)
    req = cj.build_request("rm -rf ~", "/home/u", user_messages=["clean tmp"])
    out = cj.judge_command(req, jb.get())
    assert "/new" in _Stub.hits and "/stream" in _Stub.hits
    assert _Stub.hits.index("/new") < _Stub.hits.index("/stream")
    assert out["decision"] == "BLOCK_AND_RETRY" and out["source"] == "judge"
    srv.shutdown()

def test_unreachable_bridge_is_require_human_not_allow():
    os.environ["MCP_BRIDGE_PORT"] = "1"
    os.environ["MCP_JUDGE_BACKEND"] = "bridge"; importlib.reload(jb)
    out = cj.judge_command(cj.build_request("ls", "/home/u"), jb.get())
    assert out["decision"] == "REQUIRE_HUMAN" and out["source"] == "unavailable"

def test_live_roundtrip_opt_in():
    if os.environ.get("MCP_BRIDGE_LIVE") != "1":
        print("SKIP live roundtrip (set MCP_BRIDGE_LIVE=1 to run)"); return
    os.environ.pop("MCP_BRIDGE_PORT", None)
    os.environ["MCP_JUDGE_BACKEND"] = "bridge"; importlib.reload(jb)
    assert jb.bridge_reachable(), "no bridge serving the default port"
    out = cj.judge_command(cj.build_request("git status", os.getcwd()), jb.get())
    assert out["decision"] in ("ALLOW", "BLOCK_AND_RETRY", "REQUIRE_HUMAN")
    assert out["source"] == "judge", out
    print("live verdict:", out)

if __name__ == "__main__":
    test_get_routes_bridge()
    test_fresh_conversation_before_turn_and_verdict_parses()
    test_unreachable_bridge_is_require_human_not_allow()
    test_live_roundtrip_opt_in()
    print("ok")
