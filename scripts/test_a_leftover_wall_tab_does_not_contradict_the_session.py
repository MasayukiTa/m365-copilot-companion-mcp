# -*- coding: utf-8 -*-
"""A leftover sign-in-looking tab must not make a signed-in browser "not signed in".

WHAT WAS REPORTED (2026-10-20, a colleague's PC, doctor screenshot 22). The person had signed in
and run the doctor again after waiting; it did not change:

  [FAIL] M365 signed in on the chat bridge Edge copilot-bridge-edge (:9223)
         (a sign-in page is open: https://login.microsoftonline.com/common/oauth2/v2.0/authorize)
  [FAIL] Last background start ... problem 1 of 1 (M365 sign-in needed on copilot-bridge-edge)

THE FLAW. scripts/ensure_m365_signin.py state() returned "not signed in" as soon as ANY tab looked
like a wall, before it looked at anything else. The authorize endpoint stays open as a background
tab after an authentication that finished, and is the transient URL of a silent token request
that is still in flight, so one such tab outvoted a settled M365 page and a bridge that was
answering. Two more slips fed it: an M365 tab was recognised by "m365|copilot" anywhere in the URL
(the authorize URL's own redirect_uri=https://m365... qualified), and nothing asked the bridge.

THE RULE NOW. wall only -> sign in. Tabs that disagree and nothing proves a live session ->
"cannot tell", listing every tab (bare URLs). A settled M365 page plus the bridge's own
GET /status socket_live -> signed in.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import ensure_m365_signin as m  # noqa: E402

APP = "https://m365.cloud.microsoft/chat"
MID = "https://m365.cloud.microsoft/chat/?redirfrom=CsrToSSR&auth=2"
AUTHORIZE = ("https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
             "?client_id=abc&login_hint=someone%40example.com&state=SECRETSTATE")


_REAL_STATUS_URL_FOR = m._status_url_for


@pytest.fixture(autouse=True)
def _never_ask_a_real_bridge(monkeypatch):
    """A developer machine runs a live bridge on :8765, and the default for :9223 would ask it."""
    monkeypatch.setattr(m, "_status_url_for", lambda port: "")


def _state(monkeypatch, urls, status_url=""):
    monkeypatch.setattr(m, "tabs", lambda port: [{"url": u} for u in urls])
    return m.state(9223, status_url)


@pytest.fixture
def bridge_status():
    """A stub bridge whose GET /status answers `.body`."""
    holder = {"body": {"ok": True}}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            raw = json.dumps(holder["body"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    holder["url"] = "http://127.0.0.1:%d/status" % srv.server_address[1]
    yield holder
    srv.shutdown()
    srv.server_close()


# ---- the reported tab list ----------------------------------------------------------------

def test_a_settled_app_tab_next_to_the_authorize_tab_is_not_called_not_signed_in(monkeypatch):
    ready, why = _state(monkeypatch, [APP, AUTHORIZE])
    assert ready is not False, "one leftover tab outvoted the loaded app: %s" % why
    assert ready is None, why


def test_the_authorize_tab_alone_is_still_a_wall(monkeypatch):
    ready, why = _state(monkeypatch, [AUTHORIZE])
    assert ready is False, why
    assert "a sign-in page is open" in why


def test_the_authorize_tab_next_to_a_mid_auth_app_tab_is_cannot_tell(monkeypatch):
    ready, why = _state(monkeypatch, [AUTHORIZE, MID])
    assert ready is None, why
    assert why.startswith("conflicting evidence"), why


def test_an_authorize_url_that_merely_names_m365_in_its_query_is_not_an_app_page(monkeypatch):
    """The wall's own redirect_uri is not evidence of being signed in."""
    wall = ("https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
            "?redirect_uri=https%3A%2F%2Fm365.cloud.microsoft%2Fchat")
    ready, why = _state(monkeypatch, [wall])
    assert ready is False, why


def test_a_residue_tab_naming_m365_in_its_query_is_not_an_app_page(monkeypatch):
    ready, why = _state(monkeypatch,
                        ["https://login.microsoftonline.com/savedusers?wreply=https%3A%2F%2Fm365.cloud.microsoft"])
    assert ready is None and "leftover" in why, why


# ---- the evidence that a session works ----------------------------------------------------

def test_a_settled_app_tab_and_a_live_bridge_session_make_the_wall_a_leftover(monkeypatch, bridge_status):
    bridge_status["body"] = {"ok": True, "socket_live": True, "signin_wall": False}
    ready, why = _state(monkeypatch, [APP, AUTHORIZE], bridge_status["url"])
    assert ready is True, why
    assert "leftover" in why and "?" not in why


def test_no_proof_when_the_bridge_says_it_is_stuck_on_a_wall(monkeypatch, bridge_status):
    bridge_status["body"] = {"ok": True, "socket_live": True, "signin_wall": True}
    ready, why = _state(monkeypatch, [APP, AUTHORIZE], bridge_status["url"])
    assert ready is None, why


def test_no_proof_when_the_socket_is_not_live(monkeypatch, bridge_status):
    bridge_status["body"] = {"ok": True, "socket_live": False, "transport": "socket"}
    ready, why = _state(monkeypatch, [APP, AUTHORIZE], bridge_status["url"])
    assert ready is None, why


def test_a_bridge_that_predates_the_field_is_judged_by_its_transport(monkeypatch, bridge_status):
    bridge_status["body"] = {"ok": True, "transport": "socket"}
    assert _state(monkeypatch, [APP, AUTHORIZE], bridge_status["url"])[0] is True
    bridge_status["body"] = {"ok": True, "transport": "page"}
    assert _state(monkeypatch, [APP, AUTHORIZE], bridge_status["url"])[0] is None


def test_a_live_session_alone_does_not_turn_a_lone_wall_into_signed_in(monkeypatch, bridge_status):
    """The tab says sign in, the bridge says it works, and no app page settles it: cannot tell."""
    bridge_status["body"] = {"ok": True, "socket_live": True}
    ready, why = _state(monkeypatch, [AUTHORIZE], bridge_status["url"])
    assert ready is None, why
    assert "reports a live session" in why


def test_an_unreachable_bridge_is_no_proof_and_no_crash(monkeypatch):
    ready, why = _state(monkeypatch, [APP, AUTHORIZE], "http://127.0.0.1:9/status")
    assert ready is None, why


def test_only_the_bridges_browser_is_asked_the_bridge(monkeypatch):
    monkeypatch.delenv("MCP_BRIDGE_CDP_PORT", raising=False)
    monkeypatch.delenv("MCP_BRIDGE_PORT", raising=False)
    f = _REAL_STATUS_URL_FOR
    assert f(9223) == "http://127.0.0.1:8765/status"
    assert f(9222) == ""
    monkeypatch.setenv("MCP_BRIDGE_CDP_PORT", "9333")
    monkeypatch.setenv("MCP_BRIDGE_PORT", "8800")
    assert f(9333) == "http://127.0.0.1:8800/status"
    assert f(9223) == ""


# ---- the message names itself -------------------------------------------------------------

def test_the_conflict_lists_every_tab_bare_with_a_count(monkeypatch):
    ready, why = _state(monkeypatch, [APP, AUTHORIZE, "about:blank"])
    assert ready is None
    assert "3 tab(s)" in why
    assert "settled M365 page: https://m365.cloud.microsoft/chat" in why
    assert "sign-in page: https://login.microsoftonline.com/common/oauth2/v2.0/authorize" in why
    assert "about:blank" in why
    for leaked in ("?", "login_hint", "someone", "SECRETSTATE", "client_id"):
        assert leaked not in why, "the query reached the console: %r" % leaked


def test_the_cli_prints_a_cannot_tell_verdict_for_the_conflict(monkeypatch, capsys):
    monkeypatch.setattr(m, "tabs", lambda port: [{"url": u} for u in (APP, AUTHORIZE)])
    assert m.main(["--port", "9223", "--check-only"]) == 2
    out = capsys.readouterr().out
    assert "cannot tell (conflicting evidence" in out
    assert "VERDICT: cannot_tell" in out


def test_the_profile_report_carries_the_conflict(monkeypatch, capsys):
    monkeypatch.setattr(m, "tabs", lambda port: [{"url": u} for u in (APP, AUTHORIZE)])
    m._report_every_profile(9222)
    out = capsys.readouterr().out
    assert "PROFILE: 9223 " in out and "cannot_tell" in out and "conflicting evidence" in out


# ---- the doctor's lines -------------------------------------------------------------------

def test_the_doctor_shows_the_conflict_as_info_with_the_tab_list(tmp_path):
    from test_bridge_signin_is_brought_to_the_person import _doctor_rows
    reason = ("conflicting evidence on :9223, 2 tab(s) [settled M365 page: https://m365.cloud.microsoft/chat; "
              "sign-in page: https://login.microsoftonline.com/common/oauth2/v2.0/authorize]; the bridge "
              "does not report a live session, so nothing proves whether this browser is signed in")
    rows = _doctor_rows(tmp_path, ["  PROFILE: 9223 copilot-bridge-edge cannot_tell (%s)" % reason])
    ok, optional, fix = rows["m365_signin_9223"]
    assert ok is False
    assert "2 tab(s)" in fix and "https://login.microsoftonline.com/common/oauth2/v2.0/authorize" in fix, fix
    assert "?" not in fix


def test_the_doctor_stays_silent_about_an_ordinary_cannot_tell(tmp_path):
    from test_bridge_signin_is_brought_to_the_person import _doctor_rows
    rows = _doctor_rows(tmp_path, [
        "  PROFILE: 9222 copilot-companion-edge signed_in [primary] (fine)",
        "  PROFILE: 9223 copilot-bridge-edge cannot_tell (no M365 page open, so nothing to judge from)",
        "  PROFILE: 9224 copilot-eval-edge signed_in (fine)",
    ])
    assert "m365_signin_9223" not in rows and "m365_signin_9224" in rows


def test_the_bridge_publishes_socket_live_on_status():
    import ast
    tree = ast.parse(open(os.path.join(REPO, "bridge", "copilot_bridge.py"), encoding="utf-8").read())
    keys = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "socket_live"]
    assert keys, "/status no longer carries socket_live, which the sign-in verdict reads"
