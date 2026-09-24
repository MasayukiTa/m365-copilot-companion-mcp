# -*- coding: utf-8 -*-
"""The bridge's browser on a sign-in page is brought to the person -- once, never mid-turn.

WHAT WAS REPORTED (2026-09-24, a freshly set-up second PC). doctor:
  [WARN] M365 signed in on copilot-bridge-edge (:9223) (optional) -- a sign-in page is open:
         https://<company STS>/adfs/ls/ ...
  [FAIL] Last background start ... problem 1 of 1 -- M365 sign-in needed on copilot-bridge-edge
The owner: the background (headless) tab never came to the foreground, so the person could not
sign in without typing a command. "これバグでしょ。" It was, three times over:

  1. "on a sign-in page" had four definitions; only the doctor's knew AD FS (/adfs/ls/). Every
     path that could show the window matched login.microsoftonline / login.live.com only.
  2. start_bridge.ps1's supervisor blocked on the bridge process for its whole life and looked
     for a wall only after it EXITED -- and a bridge on a wall does not exit.
  3. relay/edge_recover.surface(port=9223) ran the launcher with no -Profile, so the launcher
     acted on the FLEET's browser (default profile) instead of the bridge's.

These tests run the real code against stub browsers: a stub CDP /json and a stub bridge
/status on ephemeral ports, and -- on Windows -- the supervisor's own PowerShell functions,
extracted from scripts/start_bridge.ps1 by AST, with only Edge launching stubbed out.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import ensure_m365_signin as m  # noqa: E402
from relay import edge_auth  # noqa: E402

ADFS = "https://sts.example.com/adfs/ls/?client-request-id=x&wa=wsignin1.0&wtrealm=urn%3afederation%3aMicrosoftOnline&username=a%40example.com"
APP = "https://m365.cloud.microsoft/chat"


# ---------------------------------------------------------------------------------------------
# Stub browser (CDP /json) and stub bridge (/status)
# ---------------------------------------------------------------------------------------------

class _Stub:
    """One HTTP server playing both parts. `tabs` is what /json returns; `status` is /status.
    GET /_visible switches the tab list to the signed-in app after `signin_after` more /json
    reads -- the person signing in, as the headed window's tab list would show it."""

    def __init__(self):
        self.tabs = []
        self.status = {"ok": True, "turn_running": False, "busy": False}
        self.visible_hits = 0
        self.json_hits = 0
        self._switch_at = None
        self.signin_after = 2
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/json"):
                    stub.json_hits += 1
                    if stub._switch_at is not None and stub.json_hits >= stub._switch_at:
                        stub.tabs = [APP]
                    body = [{"url": u, "type": "page"} for u in stub.tabs]
                elif self.path.startswith("/status"):
                    body = stub.status
                elif self.path.startswith("/_visible"):
                    stub.visible_hits += 1
                    stub._switch_at = stub.json_hits + stub.signin_after
                    body = {"ok": True}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def status_url(self):
        return "http://127.0.0.1:%d/status" % self.port

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def stub():
    s = _Stub()
    yield s
    s.close()


@pytest.fixture
def latch(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SIGNIN_LATCH_DIR", str(tmp_path))
    return tmp_path


def _decide(stub, began=0.0, now=None):
    return m.bridge_signin_decision(stub.port, stub.status_url, now=now,
                                    last_start_began=began)[0]


# ---------------------------------------------------------------------------------------------
# 3. Detection is generic -- any IdP, and residue is not a wall
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    ADFS,                                                              # the reported page
    "https://sts.example.com/adfs/ls/",
    "https://adfs.example.com/adfs/oauth2/authorize?client_id=x",
    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x",
    "https://login.microsoftonline.com/organizations/saml2?SAMLRequest=abc",
    "https://login.live.com/oauth20_authorize.srf?login_hint=a%40example.com",
    "https://example.okta.com/app/office365/x/sso/wsfed/passive?wa=wsignin1.0",
    "https://idp.example.com/idp/SSO.saml2?SAMLRequest=abc",
    "https://sso.example.com/as/authorization.oauth2?client_id=x",
    "https://accounts.google.com/o/saml2/idp?SAMLRequest=x",
])
def test_every_identity_providers_sign_in_page_is_a_wall(url):
    assert edge_auth.looks_like_signin_wall(url), url
    assert edge_auth._looks_like_signin(url), url


@pytest.mark.parametrize("url", [
    APP,
    "https://m365.cloud.microsoft/chat/?redirfrom=CsrToSSR&auth=2",   # F4: authenticated bounce
    "https://login.live.com/Me.srf?wa=wsignin1.0",                     # residue (4a9ea8c)
    "https://login.microsoftonline.com/savedusers?wreply=x",            # residue (4a9ea8c)
    "about:blank",
    "",
    None,
])
def test_the_app_and_auth_residue_are_not_walls(url):
    assert not edge_auth.looks_like_signin_wall(url), url


def test_the_checker_the_doctor_reads_calls_adfs_a_wall(stub):
    stub.tabs = [ADFS]
    ready, why = m.state(stub.port)
    assert ready is False
    # and it still drops the query (login_hint / username are e-mail addresses)
    assert "adfs/ls" in why and "username" not in why and "?" not in why


def test_the_bridge_uses_the_same_definition():
    """The bridge's own wall test is edge_auth's, not edge_recover.looks_like_login (which does
    not know AD FS). Imported as a module, so this is the running function, not its text."""
    import importlib
    cb = importlib.import_module("bridge.copilot_bridge")
    assert cb._is_signin_wall(ADFS) is True
    assert cb._is_signin_wall(APP) is False
    # a goto of the bare agent URL that ends on AD FS is NOT "settled"
    assert cb._looks_redirected(ADFS, "https://m365.cloud.microsoft/chat") is True


# ---------------------------------------------------------------------------------------------
# 1. The decision: once per need, never mid-turn, nothing when signed in
# ---------------------------------------------------------------------------------------------

def test_a_wall_is_surfaced_once_per_need(stub, latch):
    stub.tabs = [ADFS]
    assert _decide(stub) == "surface"
    for _ in range(3):
        assert _decide(stub) == "already_surfaced", "the window would come back every poll"


def test_signed_in_does_nothing_and_ends_the_episode(stub, latch):
    stub.tabs = [ADFS]
    assert _decide(stub) == "surface"
    stub.tabs = [APP]
    assert _decide(stub) == "signed_in"
    assert not os.listdir(latch), "a confirmed sign-in must re-arm for the NEXT expiry"
    stub.tabs = [ADFS]
    assert _decide(stub) == "surface", "the next time the session expires, show it again"


def test_a_signed_in_bridge_is_left_alone(stub, latch):
    stub.tabs = [APP]
    assert _decide(stub) == "signed_in"
    stub.tabs = ["about:blank"]            # a healthy bridge after startup releases its page
    assert _decide(stub) == "no_wall"
    assert not os.listdir(latch)


def test_a_live_turn_defers_and_does_not_use_up_the_one_surfacing(stub, latch):
    stub.tabs = [ADFS]
    stub.status = {"ok": True, "turn_running": True, "busy": False}
    assert _decide(stub) == "deferred_turn_live"
    stub.status = {"ok": True, "turn_running": False, "busy": True}
    assert _decide(stub) == "deferred_turn_live"
    assert not os.listdir(latch), "a deferral must not count as having shown the window"
    stub.status = {"ok": True, "turn_running": False, "busy": False}
    assert _decide(stub) == "surface"


def test_a_wall_the_bridge_met_at_startup_counts_after_the_tab_is_gone(stub, latch):
    """Startup closes its page, so the tab list shows about:blank; /status still says it."""
    stub.tabs = ["about:blank"]
    stub.status = {"ok": True, "turn_running": False, "busy": False, "signin_wall": True,
                   "signin_wall_url": ""}
    assert _decide(stub) == "surface"


def test_residue_alone_is_not_a_reason_to_show_anything(stub, latch):
    stub.tabs = ["https://login.live.com/Me.srf?wa=wsignin1.0", "about:blank"]
    assert _decide(stub) == "no_wall"


def test_starting_the_app_again_shows_it_again(stub, latch):
    """The person who was away gets the window back by starting the app, not by a command.
    The run that launched the bridge began BEFORE the window was shown, so it does not."""
    stub.tabs = [ADFS]
    assert _decide(stub, began=100.0, now=200.0) == "surface"
    assert _decide(stub, began=100.0, now=210.0) == "already_surfaced"
    assert _decide(stub, began=300.0, now=310.0) == "surface"


def test_the_run_record_is_read_for_when_the_last_start_began(tmp_path):
    logs = tmp_path / ".setup" / "logs"
    logs.mkdir(parents=True)
    (logs / "start_all_runs.jsonl").write_text(
        '{"ts":"2026-09-24T10:04:09.156+09:00","end":"x"}\n'
        '{"ts":"2026-09-24T11:33:09.875+09:00","end":"y"}\n', encoding="utf-8")
    import datetime
    want = datetime.datetime.fromisoformat("2026-09-24T11:33:09.875+09:00").timestamp()
    assert m._last_start_all_began(str(tmp_path)) == want
    assert m._last_start_all_began(str(tmp_path / "nope")) == 0.0


def test_the_cli_prints_one_decision_line(stub, latch, capsys):
    stub.tabs = [ADFS]
    assert m.main(["--port", str(stub.port), "--bridge-watch", "--status-url",
                   stub.status_url]) == 0
    out = capsys.readouterr().out
    assert "DECISION: surface" in out
    assert m.main(["--port", str(stub.port), "--rearm"]) == 0
    assert not os.listdir(latch)


# ---------------------------------------------------------------------------------------------
# 1 (end to end). The supervisor's real PowerShell, against the stubs
# ---------------------------------------------------------------------------------------------

_SUPERVISOR_FUNCS = ("Invoke-SignInHelper", "Needs-SignIn", "Get-SignInDecision",
                     "Show-SignIn", "Demote-ToHeadless", "Run-BridgeWatched")

_HARNESS = r'''
$ErrorActionPreference = "Stop"
$src = "__SRC__"
$tok = $null; $err = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($src, [ref]$tok, [ref]$err)
$want = @(__FUNCS__)
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($want -contains $f.Name) { . ([scriptblock]::Create($f.Extent.Text)) }
}
# --- what start_bridge.ps1 sets at script scope ---
$root = "__TMP__"
$CdpPort = __CDP__
$BridgePort = __CDP__
$py = "__PY__"
$script:signinPy = "__SIGNIN__"
$bridge = "__FAKEBRIDGE__"
$bridgeArgs = @()
$SignInPollSec = 1
New-Item -ItemType Directory -Force (Join-Path $root ".fleet") | Out-Null
# --- the only stubs: launching Edge, and "is it headed / answering" ---
$script:calls = @()
function Ensure-Edge([switch]$Hard, [switch]$Visible, [string]$Url = "") {
    $script:calls += ("Ensure-Edge Hard=" + [bool]$Hard + " Visible=" + [bool]$Visible + " Url=" + $Url)
    if ($Visible) { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$CdpPort/_visible" | Out-Null }
    return $true
}
function Edge-IsHeaded { return $true }
function Test-Cdp { return $true }
$rc = Run-BridgeWatched
foreach ($c in $script:calls) { Write-Output ("CALL: " + $c) }
Write-Output ("RC: " + $rc)
'''

_FAKE_BRIDGE = r'''
import os, sys, time
open(os.environ["FAKE_BRIDGE_PIDFILE"], "w").write(str(os.getpid()))
time.sleep(float(os.environ.get("FAKE_BRIDGE_LIFETIME", "120")))
'''


def _run_supervisor(tmp_path, stub, lifetime):
    import shutil
    if os.name != "nt" or not shutil.which("powershell"):
        pytest.skip("the supervisor is Windows PowerShell")
    fake = tmp_path / "fake_bridge.py"
    fake.write_text(_FAKE_BRIDGE, encoding="utf-8")
    pidfile = tmp_path / "bridge.pid"
    ps = (_HARNESS
          .replace("__SRC__", os.path.join(REPO, "scripts", "start_bridge.ps1"))
          .replace("__FUNCS__", ",".join('"%s"' % f for f in _SUPERVISOR_FUNCS))
          .replace("__TMP__", str(tmp_path))
          .replace("__CDP__", str(stub.port))
          .replace("__PY__", sys.executable)
          .replace("__SIGNIN__", os.path.join(REPO, "scripts", "ensure_m365_signin.py"))
          .replace("__FAKEBRIDGE__", str(fake)))
    harness = tmp_path / "harness.ps1"
    harness.write_text(ps, encoding="utf-8-sig")
    env = dict(os.environ, MCP_SIGNIN_LATCH_DIR=str(tmp_path),
               FAKE_BRIDGE_PIDFILE=str(pidfile), FAKE_BRIDGE_LIFETIME=str(lifetime),
               NO_PROXY="127.0.0.1,localhost")
    t0 = time.time()
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(harness)], capture_output=True, timeout=240, env=env)
    out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    calls = [l[6:].strip() for l in out.splitlines() if l.startswith("CALL: ")]
    pid = int(pidfile.read_text()) if pidfile.exists() else None
    return out, calls, pid, time.time() - t0


def _alive(pid):
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie"
    except Exception:
        return False


def test_supervisor_brings_a_headless_bridge_edge_on_adfs_forward_once(tmp_path, stub):
    """The reported case, end to end: the bridge is running (it does not exit on a wall), its
    Edge is on AD FS, nothing else is happening. The supervisor must, while the bridge runs:
    stop the bridge, relaunch its Edge VISIBLE on the sign-in, wait for the person, put the Edge
    back to headless -- exactly once."""
    stub.tabs = [ADFS]
    out, calls, pid, took = _run_supervisor(tmp_path, stub, lifetime=120)
    visible = [c for c in calls if "Visible=True" in c]
    assert len(visible) == 1, out
    assert "Url=https://m365.cloud.microsoft/chat" in visible[0], visible
    assert calls[-1].startswith("Ensure-Edge Hard=True Visible=False"), \
        "the Edge was not returned to headless after the sign-in: %s" % calls
    assert calls.index(visible[0]) < len(calls) - 1
    assert pid and not _alive(pid), "the bridge child (its whole tree) was left running"
    assert took < 110, "the supervisor waited for the bridge to exit instead of acting: %.0fs" % took
    assert "RC: 0" in out, out
    assert not os.path.exists(tmp_path / ("signin_surfaced_%d.json" % stub.port)), \
        "a completed sign-in must re-arm for the next expiry"


def test_supervisor_does_not_surface_twice_for_one_need(tmp_path, stub):
    """Second bridge run on the same unresolved wall: already shown, so nothing."""
    stub.tabs = [ADFS]
    (tmp_path / ("signin_surfaced_%d.json" % stub.port)).write_text(
        json.dumps({"t": time.time(), "why": "earlier"}), encoding="utf-8")
    out, calls, pid, _ = _run_supervisor(tmp_path, stub, lifetime=4)
    assert not [c for c in calls if "Visible=True" in c], out
    assert stub.visible_hits == 0


def test_supervisor_defers_while_a_turn_is_live(tmp_path, stub):
    stub.tabs = [ADFS]
    stub.status = {"ok": True, "turn_running": True, "busy": True}
    out, calls, pid, _ = _run_supervisor(tmp_path, stub, lifetime=4)
    assert not calls, "the window was brought forward in the middle of a turn: %s" % calls
    assert not os.path.exists(tmp_path / ("signin_surfaced_%d.json" % stub.port))


def test_supervisor_leaves_a_signed_in_bridge_alone(tmp_path, stub):
    stub.tabs = [APP]
    out, calls, pid, _ = _run_supervisor(tmp_path, stub, lifetime=4)
    assert not calls, out
    assert "RC: 0" in out, out


# ---------------------------------------------------------------------------------------------
# 2. One verdict: the doctor and the start summary agree that the bridge's sign-in is required
# ---------------------------------------------------------------------------------------------

def _doctor_rows(tmp_path, report_lines, bridge_port="9223"):
    """Run the doctor's own PROFILE-row block (extracted by text between its markers) against a
    fixed checker report, with Check stubbed to record what it was told."""
    import shutil
    if os.name != "nt" or not shutil.which("powershell"):
        pytest.skip("doctor.ps1 is Windows PowerShell")
    src = open(os.path.join(REPO, "scripts", "doctor.ps1"), encoding="utf-8").read()
    start = src.index("$bridgeCdpPort = ")
    end = src.index("# 5. Bridge Edge (:9223)")
    block = src[start:end]
    ps = (
        '$envv = @{}\n'
        '$env:MCP_BRIDGE_CDP_PORT = "%s"\n'
        '$signinOut = @"\n%s\n"@\n'
        'function Check([string]$id, [string]$name, [scriptblock]$test, [string]$fix, [switch]$Optional, [switch]$Info) {\n'
        '  $ok = [bool](& $test)\n'
        '  Write-Output ("ROW|" + $id + "|" + $ok + "|" + $Optional.IsPresent + "|" + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($fix)))\n'
        '}\n' % (bridge_port, "\n".join(report_lines))) + block
    h = tmp_path / "doctor_rows.ps1"
    h.write_text(ps, encoding="ascii")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(h)],
                       capture_output=True, timeout=60)
    import base64
    rows = {}
    for l in r.stdout.decode("utf-8", "replace").splitlines():
        if l.startswith("ROW|"):
            _, rid, ok, opt, fix = l.split("|", 4)
            rows[rid] = (ok == "True", opt == "True", base64.b64decode(fix).decode("utf-8"))
    assert rows or not report_lines, r.stdout + r.stderr
    return rows


def test_doctor_calls_the_bridge_sign_in_required_and_says_what_to_do_in_japanese(tmp_path):
    rows = _doctor_rows(tmp_path, [
        "  PROFILE: 9222 copilot-companion-edge signed_in [primary] (fine)",
        "  PROFILE: 9223 copilot-bridge-edge sign_in_needed (a sign-in page is open: https://sts.example.com/adfs/ls/)",
        "  PROFILE: 9224 copilot-eval-edge sign_in_needed (a sign-in page is open: https://login.microsoftonline.com/x)",
    ])
    ok, optional, fix = rows["m365_signin_9223"]
    assert ok is False and optional is False, \
        "the chat window's browser was called optional while start_all counts it as a problem"
    assert "サインインしてください" in fix and "前面に表示" in fix, fix
    assert "powershell" not in fix.lower(), "a non-engineer was handed a command: %s" % fix
    # the evaluation browser is a measurement tool: still optional
    assert rows["m365_signin_9224"][1] is True


def test_doctor_says_the_same_thing_about_the_last_start_line(tmp_path):
    """start_all's "problem 1 of 1" line and the row above are one fact; the reported screen
    showed a WARN with a PowerShell command and a FAIL with a fragment. The last-start row now
    carries the same Japanese instruction -- and reads the file as UTF-8, so it would survive
    start_all writing Japanese into it too."""
    import shutil
    if os.name != "nt" or not shutil.which("powershell"):
        pytest.skip("doctor.ps1 is Windows PowerShell")
    src = open(os.path.join(REPO, "scripts", "doctor.ps1"), encoding="utf-8").read()
    rows_block = src[src.index("$bridgeCdpPort = "):src.index("# 5. Bridge Edge (:9223)")]
    last_block = src[src.index("function Get-LastStartSummaryDoctor"):
                     src.index('Write-Host "---------------------------------------------"')]
    logs = tmp_path / ".setup" / "logs"
    logs.mkdir(parents=True)
    (logs / "start_all_summary.txt").write_bytes(
        ("failures=2\nwhen=2026-09-24 11:35:08\nmode=full\n"
         "- M365 sign-in needed on copilot-bridge-edge (:9223)\n"
         "- bridge: 起動に失敗\n").encode("utf-8"))
    ps = ('$envv = @{}\n$env:MCP_BRIDGE_CDP_PORT = ""\n$signinOut = ""\n$repo = "%s"\n'
          'function Invoke-RestMethod { throw "stub" }\n'
          'function Check([string]$id, [string]$name, [scriptblock]$test, [string]$fix, [switch]$Optional, [switch]$Info) {\n'
          '  Write-Output ("ROW|" + $id + "|" + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($fix)))\n'
          '}\n' % tmp_path) + rows_block + last_block
    h = tmp_path / "doctor_last.ps1"
    h.write_text(ps, encoding="ascii")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(h)],
                       capture_output=True, timeout=60)
    import base64
    rows = {}
    for l in r.stdout.decode("utf-8", "replace").splitlines():
        if l.startswith("ROW|"):
            _, rid, fix = l.split("|", 2)
            rows[rid] = base64.b64decode(fix).decode("utf-8")
    assert "last_start_1" in rows, r.stdout + r.stderr
    assert "サインインしてください" in rows["last_start_1"], rows
    assert rows["last_start_2"] == "bridge: 起動に失敗", \
        "a UTF-8 line from start_all came out garbled: %r" % rows["last_start_2"]


def test_doctor_follows_the_bridge_port_setting(tmp_path):
    rows = _doctor_rows(tmp_path, [
        "  PROFILE: 9223 copilot-bridge-edge sign_in_needed (x)",
        "  PROFILE: 9333 copilot-bridge-edge sign_in_needed (x)",
    ], bridge_port="9333")
    assert rows["m365_signin_9333"][1] is False
    assert rows["m365_signin_9223"][1] is True


# ---------------------------------------------------------------------------------------------
# The launcher acts on the browser that is on the port it was given
# ---------------------------------------------------------------------------------------------

def test_the_way_back_to_background_names_the_bridge_profile(monkeypatch):
    """edge_auth's rehide timer relaunched with "-Port 9223" and no -Profile: the launcher's
    default profile is the FLEET's, so it hard-reset the wrong browser."""
    import relay.edge_recover as rec
    seen = {}
    monkeypatch.setattr(rec, "surface", lambda port=None, open_url="": True)
    monkeypatch.setattr(rec, "rehide", lambda port=None: None)
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: seen.setdefault("argv", argv))

    class _T:
        def __init__(self, d, fn):
            self.fn = fn
            self.daemon = True

        def start(self):
            self.fn()

    monkeypatch.setattr(threading, "Timer", _T)
    edge_auth._surface_with_a_way_back("http://127.0.0.1:9223", "https://x", 0.0)
    argv = seen["argv"]
    assert argv[argv.index("-Profile") + 1] == rec.MANAGED_EDGE_PROFILES[9223], argv


def test_the_launcher_derives_the_profile_from_the_port_when_not_told():
    src = open(os.path.join(REPO, "scripts", "start_companion_edge.ps1"), encoding="utf-8-sig").read()
    i = src.index("$PSBoundParameters.ContainsKey('Profile')")
    assert i < src.index("$dataDir = Join-Path $env:LOCALAPPDATA $Profile"), \
        "the profile must be settled before anything uses it"
    assert "--remote-debugging-port=" in src[i:i + 900]
