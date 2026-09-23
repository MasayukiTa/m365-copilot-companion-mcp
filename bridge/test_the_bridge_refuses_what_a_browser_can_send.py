# -*- coding: utf-8 -*-
"""The bridge's door, EXECUTED: who is let in, who is not, and that the clients get through.

## What was wrong (SEC-04, 2026-09-24)

Every endpoint of bridge/copilot_bridge.py was a bare GET with no check. /stream and /goal start
Copilot turns in the signed-in session, /send queues one, /delete and /forget remove
conversations, /upload attaches a local file -- and any web page open in any browser on this
machine could make the browser send any of them to 127.0.0.1 (an <img src> is enough), as could
any local process. Binding to loopback is not an access check.

## What is checked here, by running the real Handler

A throwaway server on a free port -- never the live bridge on :8765, whose page endpoints have
set off an Edge self-harm loop before. The page layer is stubbed out: `run_on_page_thread`
records and raises, so even with a guard mutated away no request can reach a browser.

* no token -> 401; wrong token -> 401; token not installed -> 503 (fail closed)
* Origin / Referer / cross-site Sec-Fetch-Site / non-loopback Host -> 403, even WITH the token
* GET on anything that changes state or reads the page -> 405, and the reason phrase an old
  client shows the person says what to do
* OPTIONS -> 403 with no Access-Control-* header (never a positive preflight)
* the right token on a POST -> accepted, and the handler really ran
* /status and /conv answer a token-free probe, withholding the conversation ids / url
* every migrated Python client (bridge_auth.request, session_cli, companionbench's BridgeAgent)
  gets through, re-reads the token after a restart, and fails with an actionable message
  against a missing token file or an old (GET-only) bridge
* the token file is owner-only, read back from icacls
"""
from __future__ import annotations

import http.client
import http.server
import inspect
import json
import os
import sys
import threading
import urllib.parse

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bridge import bridge_auth  # noqa: E402


class _PageTouched(AssertionError):
    pass


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    """(base_url, module, page_calls) for a real Handler on a free port with a real token."""
    monkeypatch.setenv("MCP_SESSION_STORE_DIR", str(tmp_path / "sessions"))
    from bridge import session_store as S
    monkeypatch.setattr(S, "SESS_DIR", str(tmp_path / "sessions"), raising=False)
    assert S._base_dir() == str(tmp_path / "sessions"), "the store is not isolated"

    import bridge.copilot_bridge as B
    # No page and no driver, whatever an earlier test in this process left behind (several
    # install fakes on the module); monkeypatch puts theirs back afterwards.
    monkeypatch.setattr(B, "PAGE", None)
    monkeypatch.setattr(B, "DRIVER", None)
    page_calls = []

    def _no_page(fn, *a, **kw):
        page_calls.append(getattr(fn, "__name__", repr(fn)))
        raise _PageTouched("page work attempted in a hermetic bridge: %r" % (fn,))

    monkeypatch.setattr(B, "run_on_page_thread", _no_page)
    monkeypatch.setattr(B, "STOP_REQUESTED", False)
    monkeypatch.setenv(bridge_auth.TOKEN_DIR_ENV, str(tmp_path / "token"))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), B.Handler)
    token, _ = bridge_auth.install_token(srv.server_address[1])
    monkeypatch.setattr(B, "BRIDGE_TOKEN", token)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1], B, page_calls
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _raw(base, method, path, headers=None, form=None):
    """(status, reason, response headers, body text) -- headers exactly as given, nothing added
    but http.client's Host (unless a Host is given) and Content-Length."""
    u = urllib.parse.urlparse(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=15)
    body = form.encode("ascii") if form is not None else None
    hdrs = dict(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    try:
        c.request(method, path, body=body, headers=hdrs)
        r = c.getresponse()
        return r.status, r.reason, dict(r.getheaders()), r.read().decode("utf-8", "replace")
    finally:
        c.close()


def _tok(base):
    return {bridge_auth.TOKEN_HEADER: bridge_auth.read_token(bridge_auth.port_of(base))}


def _pending(sid):
    from bridge import session_store as S
    return (S.load(sid) or {}).get("pending") or []


#: Every route that needs the token, i.e. every route but the three token-free probes.
def _guarded(B):
    return sorted(B.BRIDGE_ROUTES - B.BRIDGE_OPEN_ROUTES)


# ── refused ─────────────────────────────────────────────────────────────────────────────────

def test_no_token_is_refused_on_every_guarded_route(bridge):
    base, B, page = bridge
    for path in _guarded(B):
        st, _, _, body = _raw(base, "POST", path, form="msg=hi&text=hi")
        assert st == 401, (path, st, body)
        assert json.loads(body)["ok"] is False
    assert page == [], "a refused request still reached page code: %r" % page


def test_a_wrong_token_is_refused_on_every_guarded_route(bridge):
    base, B, page = bridge
    for path in _guarded(B):
        st, reason, _, _ = _raw(base, "POST", path, {bridge_auth.TOKEN_HEADER: "not-the-token"},
                                form="msg=hi")
        assert st == 401 and "wrong" in reason.lower(), (path, st, reason)
    assert page == []


def test_a_wrong_token_is_refused_even_on_a_token_free_probe(bridge):
    """A token that is PRESENT but wrong is a caller holding a stale token -- telling it 200
    would hide the restart it needs to notice."""
    base, _, _ = bridge
    st, _, _, _ = _raw(base, "GET", "/status", {bridge_auth.TOKEN_HEADER: "stale"})
    assert st == 401


def test_a_token_that_was_never_installed_fails_closed(bridge, monkeypatch):
    base, B, page = bridge
    monkeypatch.setattr(B, "BRIDGE_TOKEN", None)
    st, _, _, _ = _raw(base, "POST", "/send", {bridge_auth.TOKEN_HEADER: ""}, form="msg=hi")
    assert st == 503
    st, _, _, _ = _raw(base, "POST", "/send", form="msg=hi")
    assert st == 503
    assert page == []


@pytest.mark.parametrize("header,value", [
    ("Origin", "https://evil.example"),
    ("Origin", "null"),
    ("Origin", "http://127.0.0.1:8765"),
    ("Referer", "https://evil.example/page"),
    ("Sec-Fetch-Site", "cross-site"),
    ("Sec-Fetch-Site", "same-site"),
])
def test_browser_traffic_is_refused_even_with_the_right_token(bridge, header, value):
    base, B, page = bridge
    h = _tok(base)
    h[header] = value
    for method, path in (("POST", "/send"), ("POST", "/stream"), ("GET", "/status"),
                         ("GET", "/conv"), ("GET", "/")):
        st, _, _, _ = _raw(base, method, path, h, form="msg=hi" if method == "POST" else None)
        assert st == 403, (header, value, method, path, st)
    assert page == []


@pytest.mark.parametrize("value", ["none", "same-origin"])
def test_sec_fetch_site_none_and_same_origin_are_not_refused(bridge, value):
    base, _, _ = bridge
    h = _tok(base)
    h["Sec-Fetch-Site"] = value
    st, _, _, body = _raw(base, "POST", "/send", h, form="msg=hi")
    assert st == 200 and json.loads(body)["ok"] is True


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8765", "127.0.0.1.nip.io:8765",
                                  "attacker.test:80"])
def test_a_non_loopback_host_is_refused(bridge, host):
    """DNS rebinding: a name that resolves to 127.0.0.1 reaches the socket but carries its own
    name in Host."""
    base, _, page = bridge
    h = _tok(base)
    h["Host"] = host
    for method, path in (("POST", "/send"), ("GET", "/status")):
        st, _, _, _ = _raw(base, method, path, h, form="msg=hi" if method == "POST" else None)
        assert st == 403, (host, path, st)
    assert page == []


def test_loopback_host_spellings_are_accepted(bridge):
    base, _, _ = bridge
    port = bridge_auth.port_of(base)
    for host in ("127.0.0.1:%d" % port, "localhost:%d" % port, "LOCALHOST", "[::1]:%d" % port):
        h = _tok(base)
        h["Host"] = host
        st, _, _, _ = _raw(base, "POST", "/send", h, form="msg=hi")
        assert st == 200, host


def test_get_on_every_guarded_route_is_refused_with_a_reason_an_old_client_can_act_on(bridge):
    """Refused, and nothing runs. /stream and /goal answer the refusal AS A STREAM (200, one
    "[bridge error: ...]" replace, done) because that body is the only text an old chat window
    shows -- .NET replaces a 405's reason phrase with its own localized wording. Every other
    route is a 405 whose reason phrase says what to do."""
    base, B, page = bridge
    from bridge import session_store as S
    before = len(S.list_sessions())
    for path in _guarded(B):
        for h in ({}, _tok(base)):
            st, reason, hdrs, body = _raw(base, "GET", path + "?msg=hi&text=hi", h)
            if path in ("/stream", "/goal"):
                assert st == 200 and hdrs.get("X-Bridge-Refused") == "405", (path, st)
                assert hdrs.get("Content-Type", "").startswith("text/event-stream")
                assert "[bridge error:" in body and "rebuild_ui.ps1" in body, body
                assert "event: done" in body
            else:
                assert st == 405, (path, st)
                assert "POST" in reason and "rebuild_ui.ps1" in reason, reason
                assert hdrs.get("Allow") == "POST"
    assert page == [], "a refused GET reached page code: %r" % page
    assert B.STOP_REQUESTED is False, "a refused GET /stop still stopped"
    assert len(S.list_sessions()) == before, "a refused GET still created a session"


def test_a_preflight_is_never_answered_positively(bridge):
    base, B, page = bridge
    st, _, hdrs, _ = _raw(base, "OPTIONS", "/send", {
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-bridge-token"})
    assert st == 403
    st2, _, hdrs2, _ = _raw(base, "OPTIONS", "/send", _tok(base))
    assert st2 == 403
    for h in (hdrs, hdrs2):
        assert not [k for k in h if k.lower().startswith("access-control-")], h


def test_no_response_ever_carries_a_cors_header(bridge):
    base, _, _ = bridge
    for method, path, h in (("POST", "/send", _tok(base)), ("GET", "/status", {}),
                            ("POST", "/send", {})):
        _, _, hdrs, _ = _raw(base, method, path, h, form="msg=hi" if method == "POST" else None)
        assert not [k for k in hdrs if k.lower().startswith("access-control-")], hdrs


def test_a_body_that_is_not_a_form_is_refused(bridge):
    base, _, _ = bridge
    h = _tok(base)
    h["Content-Type"] = "application/json"
    st, _, _, _ = _raw(base, "POST", "/send", h, form='{"msg": "hi"}')
    assert st == 415


# ── accepted ────────────────────────────────────────────────────────────────────────────────

def test_the_right_token_on_a_post_is_accepted_and_the_handler_runs(bridge):
    base, B, page = bridge
    st, _, _, body = _raw(base, "POST", "/send", _tok(base), form="msg=" +
                          urllib.parse.quote("hello & 100% + more", safe=""))
    assert st == 200, body
    r = json.loads(body)
    assert r["ok"] is True and r["queued"] is True
    assert _pending(r["sid"])[-1] == "hello & 100% + more"

    assert B.STOP_REQUESTED is False
    st, _, _, body = _raw(base, "POST", "/stop", _tok(base))
    assert st == 200 and json.loads(body)["ok"] is True
    assert B.STOP_REQUESTED is True, "/stop answered but did not stop"

    st, _, _, body = _raw(base, "POST", "/forget", _tok(base), form="sid=" + r["sid"])
    assert st == 200 and json.loads(body)["removed_local"] is True

    st, _, _, body = _raw(base, "POST", "/sessions", _tok(base))
    assert st == 200 and "sessions" in json.loads(body)
    # /send promotes on another thread; with no page thread that attempt is what is recorded
    # (and refused by the stub) -- nothing else touched the page.
    assert set(page) <= {"_drain_pending_queue"}, page


def test_query_and_form_fields_both_arrive(bridge):
    """Fields in the URL and in the body are one set: the handlers read parse_qs(query)."""
    base, _, _ = bridge
    from bridge import session_store as S
    sid = S.new_session()["sid"]
    st, _, _, body = _raw(base, "POST", "/send?sid=" + sid, _tok(base), form="msg=from-body")
    r = json.loads(body)
    assert st == 200 and r.get("sid") == sid, body
    assert _pending(sid)[-1] == "from-body"


def test_status_without_the_token_is_liveness_only(bridge, monkeypatch):
    base, B, _ = bridge
    monkeypatch.setattr(B, "ACTIVE_SID", "sid-secret")
    st, _, _, body = _raw(base, "GET", "/status")
    r = json.loads(body)
    assert st == 200 and r["ok"] is True
    assert "turn_running" in r and "busy" in r, "the restart gates read these two"
    assert "active_sid" not in r and "conversation" not in r
    assert r["authenticated"] is False
    st, _, _, body = _raw(base, "GET", "/status", _tok(base))
    r = json.loads(body)
    assert r["active_sid"] == "sid-secret" and "conversation" in r and r["authenticated"] is True


def test_conv_without_the_token_answers_liveness_and_withholds_the_url(bridge):
    base, B, page = bridge
    # Held by "a turn": the token-free probe must still answer, and without touching the page.
    assert B.PAGE_LOCK.acquire(timeout=5)
    try:
        st, _, _, body = _raw(base, "GET", "/conv")
    finally:
        B.PAGE_LOCK.release()
    r = json.loads(body)
    assert st == 200 and r["ok"] is True and r["url"] == "" and r["url_withheld"] is True
    assert page == []


def test_the_root_page_no_longer_carries_a_chat_that_uses_get(bridge):
    base, B, _ = bridge
    st, _, _, body = _raw(base, "GET", "/")
    assert st == 200
    assert "EventSource" not in body and "/stream" not in body


# ── the Python clients ──────────────────────────────────────────────────────────────────────

def test_bridge_auth_request_gets_through(bridge):
    base, _, _ = bridge
    with bridge_auth.request(base, "/send", {"msg": "via bridge_auth"}, timeout=10) as r:
        body = json.loads(r.read().decode("utf-8"))
    assert body["ok"] is True and _pending(body["sid"])[-1] == "via bridge_auth"


def test_a_restarted_bridge_is_followed_by_rereading_the_token(bridge, monkeypatch):
    """The token rotates at every start; a client holding the old one re-reads the file once."""
    base, B, _ = bridge
    port = bridge_auth.port_of(base)
    old = bridge_auth.read_token(port)
    new, _ = bridge_auth.install_token(port)            # what a restart does
    monkeypatch.setattr(B, "BRIDGE_TOKEN", new)
    assert new != old
    # A client that cached `old` would send it first: prove the server now refuses it...
    st, _, _, _ = _raw(base, "POST", "/send", {bridge_auth.TOKEN_HEADER: old}, form="msg=x")
    assert st == 401
    # ...and that the client, reading the file, gets through.
    with bridge_auth.request(base, "/send", {"msg": "after restart"}, timeout=10) as r:
        assert json.loads(r.read().decode("utf-8"))["ok"] is True


def test_a_401_makes_the_python_client_read_the_file_again(bridge, monkeypatch):
    """The restart lands BETWEEN the client's read and its request: the first read is stale,
    the 401 must send it back to the file rather than to the caller."""
    base, B, _ = bridge
    port = bridge_auth.port_of(base)
    stale = bridge_auth.read_token(port)
    new, _ = bridge_auth.install_token(port)
    monkeypatch.setattr(B, "BRIDGE_TOKEN", new)
    real, reads = bridge_auth.read_token, []

    def _first_read_is_stale(p):
        reads.append(p)
        return stale if len(reads) == 1 else real(p)

    monkeypatch.setattr(bridge_auth, "read_token", _first_read_is_stale)
    with bridge_auth.request(base, "/send", {"msg": "raced a restart"}, timeout=10) as r:
        assert json.loads(r.read().decode("utf-8"))["ok"] is True
    assert len(reads) == 2, "the client did not go back to the file after the 401"


def test_a_token_file_the_bridge_does_not_hold_is_reported_not_retried_forever(bridge,
                                                                              monkeypatch):
    base, B, _ = bridge
    monkeypatch.setattr(B, "BRIDGE_TOKEN", "the-bridge-has-another-one")
    with pytest.raises(bridge_auth.BridgeAuthError) as e:
        bridge_auth.request(base, "/send", {"msg": "x"}, timeout=10)
    assert "401" in str(e.value) and "token" in str(e.value)


def test_no_token_file_is_an_actionable_error(bridge, monkeypatch, tmp_path):
    base, _, _ = bridge
    monkeypatch.setenv(bridge_auth.TOKEN_DIR_ENV, str(tmp_path / "empty"))
    with pytest.raises(bridge_auth.BridgeAuthError) as e:
        bridge_auth.request(base, "/send", {"msg": "x"}, timeout=10)
    msg = str(e.value)
    assert "no bridge token" in msg and "older than this client" in msg and "Restart" in msg


def test_an_old_get_only_bridge_is_an_actionable_error(tmp_path, monkeypatch):
    """New client, old bridge: BaseHTTPRequestHandler answers an unhandled POST with 501 --
    or, because it never reads the body, often with a connection reset. Both must come out as
    the same actionable message (the reset path asks /status, which the old bridge serves)."""
    class _Old(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):              # the old bridge's /status: no "authenticated" field
            out = b'{"ok": true, "transport": "page", "busy": false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Old)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(bridge_auth.TOKEN_DIR_ENV, str(tmp_path))
        bridge_auth.install_token(srv.server_address[1])   # a leftover file from a newer run
        for i in range(8):             # the 501 and the reset race; both must read the same
            with pytest.raises(bridge_auth.BridgeAuthError) as e:
                bridge_auth.request("http://127.0.0.1:%d" % srv.server_address[1], "/send",
                                    {"msg": "x" * (i * 5000)}, timeout=10)
            assert "older than this client" in str(e.value), str(e.value)
    finally:
        srv.shutdown()
        srv.server_close()


def test_session_cli_gets_through(bridge):
    base, _, _ = bridge
    from bridge import session_cli
    c = session_cli.BridgeClient(base_url=base, timeout=10)
    assert c.is_up()
    r = c.send("", "from the cli")
    assert r["ok"] is True and _pending(r["sid"])[-1] == "from the cli"
    assert isinstance(c.list_sessions(), list)


def test_companionbench_agent_gets_through(bridge):
    base, _, _ = bridge
    from bench.companionbench.agents import BridgeAgent
    a = BridgeAgent(host="127.0.0.1", port=bridge_auth.port_of(base), timeout=15)
    raw = a._request("/send?msg=" + urllib.parse.quote("from the bench"), timeout=15)
    head, _, body = raw.partition("\r\n\r\n")
    assert head.startswith("HTTP/1.0 200"), head
    assert _pending(json.loads(body)["sid"])[-1] == "from the bench"


# ── the token file ──────────────────────────────────────────────────────────────────────────

def test_the_token_file_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv(bridge_auth.TOKEN_DIR_ENV, str(tmp_path))
    token, evidence = bridge_auth.install_token(40123)
    p = bridge_auth.token_path(40123)
    assert bridge_auth.read_token(40123) == token and len(token) >= 40
    if os.name == "nt":
        entries, listing = bridge_auth.acl_entries(p)
        name, _sid = bridge_auth._current_user()
        assert [e[0].lower() for e in entries] == [name.lower()], listing
        assert entries[0][1] == "(F)", listing
        assert "(I)" not in listing, "an inherited ACE survived: %s" % listing
    else:
        assert os.stat(p).st_mode & 0o777 == 0o600
    assert not [f for f in os.listdir(str(tmp_path)) if f.endswith(".tmp")], "tmp left behind"


def test_every_start_mints_a_new_token(tmp_path, monkeypatch):
    monkeypatch.setenv(bridge_auth.TOKEN_DIR_ENV, str(tmp_path))
    a, _ = bridge_auth.install_token(40124)
    b, _ = bridge_auth.install_token(40124)
    assert a != b and bridge_auth.read_token(40124) == b


def test_the_token_is_written_after_the_bind_and_the_bridge_fails_closed_without_it():
    """A second instance that loses the bind must not overwrite the serving bridge's token.
    (Source order, because running main() means starting a browser.)"""
    import bridge.copilot_bridge as B
    src = inspect.getsource(B.main)
    assert src.index("_SingleBindHTTPServer((") < src.index("install_token(") \
        < src.index("srv.serve_forever()")
    assert "SystemExit" in src[src.index("install_token("):src.index("srv.serve_forever()")]
