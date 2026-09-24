# -*- coding: utf-8 -*-
r"""Another PC serving this PC's tunnel: the supervisor reports it instead of fighting, and
setup_devtunnel.ps1 never adopts a tunnel another machine is hosting.

THE INCIDENT (2026-09-24 08:31-08:48). A second PC signed in to the same Microsoft account hosted
this PC's tunnel. This PC's supervisor re-hosted after four misses; its host "established" (the
count it read was the other PC's) and exited 27 s later. From then on `devtunnel show` said
"Host connections : 1", the supervisor's hosting check -- connections >= 1 -- said "hosting",
and nothing re-hosted or reported while every call to this PC's URL timed out and the cockpit's
tunnel dot was red.

THE FIX, EXECUTED HERE.
  supervisor.ps1  Resolve-TunnelHostingState asks the cockpit's question -- does GET <tunnel>/health
                  answer with THIS machine's server_pid -- and tells apart ours / none (re-host as
                  before) / foreign (hosted, but by no host process of this PC) / shared (ours
                  runs, but the tunnel answered with another pid). foreign and shared are reported
                  once in the log and in .fleet\tunnel_host.json and never re-hosted; the probe
                  through the tunnel backs off 1, 2, 4 ... ticks after a failure, capped.
  setup_devtunnel.ps1  a tunnel `devtunnel show` reports as hosted while no devtunnel host on this
                  machine hosts it is not adopted -- whether the name came from .env, from
                  -TunnelName or is the bare legacy default; this machine's own default is exempt.

HOW THIS RUNS. Supervisor: its functions are extracted into a temp driver (never dot-source
supervisor.ps1: its top level takes the global mutex and starts the loop) with a stub
devtunnel.cmd reading a JSON state file, a stub local server and a stub "tunnel" server on FREE
ports (never 8000), and a temp $Root -- so the status file lands in a temp .fleet. setup: the
existing stub rig of test_setup_devtunnel_access_and_identity.py, with its `show` taught to print
the host count and its `host` able to stay alive.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from tools import childproc  # noqa: E402
import test_setup_devtunnel_access_and_identity as setup_rig  # noqa: E402

SUPERVISOR_PS1 = os.path.join(REPO, "scripts", "supervisor.ps1")
SETUP_PS1 = os.path.join(REPO, "scripts", "setup_devtunnel.ps1")
NAME_UTIL_PS1 = os.path.join(REPO, "scripts", "tunnel_name_util.ps1")

_POWERSHELL = setup_rig._POWERSHELL

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason="supervisor.ps1 / setup_devtunnel.ps1 are Windows PowerShell (os.name=%r)" % (os.name,),
)


def _extract_braced_block(text: str, start_marker: str) -> str:
    idx = text.index(start_marker)
    brace_start = text.index("{", idx)
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[idx:i + 1]
    raise AssertionError("unbalanced braces extracting %r" % (start_marker,))


def _extract_line(text: str, needle: str) -> str:
    idx = text.index(needle)
    return text[text.rfind("\n", 0, idx) + 1:text.index("\n", idx)]


def _extract_between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    # Back up to the start of the marker LINE and forward to the end of the end-marker line,
    # so the extracted block is whole lines, not the two comment lines' bare substrings.
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.index("\n", end)
    return text[line_start:line_end]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert port != 8000
    return port


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# -- the rule both scripts use to recognise "a devtunnel host of THIS tunnel" ------------------

_CMDLINES = {
    "exe": r'"D:\tools\WinGet\Links\devtunnel.exe" host abc-tunnel',
    "exeCluster": r'"C:\x\devtunnel.exe" host abc-tunnel.jpe1',
    "bare": "devtunnel host abc-tunnel",
    "cmdStub": r'C:\WINDOWS\system32\cmd.exe /c C:\t\stub\devtunnel.cmd host abc-tunnel',
    "prefixName": r'"C:\x\devtunnel.exe" host abc-tunnel-2',
    "otherVerb": r'"C:\x\devtunnel.exe" show abc-tunnel',
    "login": r'"C:\x\devtunnel.exe" user login',
    "notDevtunnel": r'"C:\py\python.exe" host abc-tunnel',
    "empty": "",
}
_EXPECT = {"exe": True, "exeCluster": True, "bare": True, "cmdStub": True, "prefixName": False,
           "otherVerb": False, "login": False, "notDevtunnel": False, "empty": False}


def test_both_scripts_recognise_this_tunnels_host_the_same_way(tmp_path):
    sup = _read(SUPERVISOR_PS1)
    # setup_devtunnel.ps1's copy moved to tunnel_name_util.ps1 (2026-09-24), which it and
    # heal_tunnel.ps1 dot-source; the comparison is now supervisor vs that shared one.
    setup = _read(NAME_UTIL_PS1)
    lines = ["$ErrorActionPreference = 'Stop'", ". '%s'" % NAME_UTIL_PS1,
             _extract_braced_block(sup, "function Test-IsTunnelHostCommandLine")]
    setup_fn = _extract_braced_block(setup, "function Test-IsTunnelHostCommandLine")
    lines.append(setup_fn.replace("function Test-IsTunnelHostCommandLine",
                                  "function Test-IsTunnelHostCommandLineSetup", 1))
    lines.append("$r = [ordered]@{}")
    for k, v in _CMDLINES.items():
        lit = v.replace("'", "''")
        lines.append("$r['%s'] = @((Test-IsTunnelHostCommandLine '%s' 'abc-tunnel'), "
                     "(Test-IsTunnelHostCommandLineSetup '%s' 'abc-tunnel'))" % (k, lit, lit))
    lines.append("$r | ConvertTo-Json | Set-Content -Path '%s' -Encoding UTF8" % (tmp_path / "o.json"))
    drv = tmp_path / "rule.ps1"
    drv.write_text("\n".join(lines), encoding="utf-8")
    proc = childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(drv)],
                         timeout=60, creationflags=childproc.headless_creationflags())
    assert proc.returncode == 0, proc.stderr
    got = json.loads((tmp_path / "o.json").read_text(encoding="utf-8-sig"))
    for k, want in _EXPECT.items():
        assert got[k] == [want, want], "%s: supervisor/setup said %r, expected %r" % (k, got[k], want)


# -- the supervisor, against stubs ----------------------------------------------------------------

_DT_STUB_PY = r'''
import json, os, sys
st = json.load(open(os.environ["SUP_STATE"], encoding="utf-8-sig"))
a = sys.argv[1:]
with open(os.environ["SUP_DT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(a) + "\n")
if a[:1] == ["show"]:
    print("Tunnel ID             : %s.jpe1" % a[1])
    if st.get("hosts") is not None:
        print("Host connections      : %d" % st["hosts"])
    sys.exit(0)
sys.exit(0)
'''

_DT_STUB_CMD = '@echo off\r\n"%SUP_PY%" "%~dp0dt_stub.py" %*\r\nexit /b %ERRORLEVEL%\r\n'

# role "local": this machine's server, pid 4242. role "tunnel": what the public URL reaches, as the
# state file says -- tunnel_status (200 / 500) and tunnel_pid (None = a 200 without a pid). Every
# request to the tunnel is counted so the backoff can be measured.
_HTTP_STUB_PY = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
role, port = sys.argv[1], int(sys.argv[2])
state_path, hits_path = os.environ["SUP_STATE"], os.environ["SUP_HITS"]
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if role == "local":
            code, doc = 200, {"status": "ok", "server_pid": 4242}
        else:
            with open(hits_path, "a", encoding="utf-8") as fh:
                fh.write("x\n")
            st = json.load(open(state_path, encoding="utf-8-sig"))
            code = int(st.get("tunnel_status", 200))
            doc = {"status": "ok"}
            if st.get("tunnel_pid") is not None:
                doc["server_pid"] = st["tunnel_pid"]
        body = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''

_SUP_DRIVER_HEAD = r'''param(
    [Parameter(Mandatory=$true)][string]$RootDir,
    [Parameter(Mandatory=$true)][string]$DtCmd,
    [Parameter(Mandatory=$true)][string]$StateFile,
    [Parameter(Mandatory=$true)][string]$HitsFile,
    [Parameter(Mandatory=$true)][int]$LocalPort,
    [Parameter(Mandatory=$true)][string]$SleeperPy,
    [Parameter(Mandatory=$true)][string]$OutFile
)
# THE SUPERVISOR'S OWN SETTING, so the extracted code runs as it does there.
$ErrorActionPreference = "SilentlyContinue"
$script:CapturedLog = New-Object System.Collections.Generic.List[string]
function Write-Log($msg) { $script:CapturedLog.Add([string]$msg) }
$Root = $RootDir
$Port = $LocalPort
$DevTunnel = $DtCmd
$TunnelName = "abc-tunnel"
$FailuresBeforeAction = 4
$script:TunnelHostProc = $null
$script:DevtunnelLoginAnswerWasCached = $false
$script:tunnelMiss = 0
$script:loggedIn = $true
$script:HostCalls = 0
function Test-DevtunnelLoggedInCached { return $true }
# RECORDS instead of launching a real `devtunnel host`: what is under test is whether the
# decision to re-host is taken, not devtunnel.
function Start-TunnelHost { $script:HostCalls++; return $true }
function Start-Sleep { param([int]$Seconds, [int]$Milliseconds) }
. (Join-Path $RootDir "tunnel_name_util.ps1")
'''

_SUP_DRIVER_BODY = r'''
function Set-Stub([object]$Hosts, [object]$TunnelPid, [int]$TunnelStatus = 200) {
    $doc = @{ hosts = $Hosts; tunnel_pid = $TunnelPid; tunnel_status = $TunnelStatus }
    [IO.File]::WriteAllText($StateFile, ($doc | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
}
function Reset-All {
    $script:tunnelMiss = 0; $script:HostCalls = 0; $script:ForeignStreak = 0; $script:TunnelState = ""
    $script:TunnelStatusKey = ""; Reset-TunnelProbeBackoff; $script:CapturedLog.Clear()
    if (Test-Path $script:TunnelStatusPath) { Remove-Item $script:TunnelStatusPath -Force }
    [IO.File]::WriteAllText($HitsFile, "")
}
function Hits { return @(Get-Content $HitsFile | Where-Object { $_ }).Count }
function Status {
    if (-not (Test-Path $script:TunnelStatusPath)) { return $null }
    return (Get-Content $script:TunnelStatusPath -Raw | ConvertFrom-Json)
}
function Ticks([int]$n) {
    $s = @()
    for ($i = 0; $i -lt $n; $i++) { $s += [string](Invoke-TunnelHostingCheck) }
    return ,$s
}
function Case([string]$Label, [int]$N) {
    $states = Ticks $N
    return [ordered]@{ label = $Label; states = $states; hostCalls = $script:HostCalls; hits = (Hits)
                       log = @($script:CapturedLog); status = (Status) }
}

$r = [ordered]@{}
$sleeper = $null
try {
    # 1. OURS: the public URL answers with this machine's pid.
    Reset-All; Set-Stub 1 4242
    $r.ours = Case "ours" 6

    # 2. NONE: nobody hosts it -> the old debounce and re-host.
    Reset-All; Set-Stub 0 $null
    $r.none = Case "none" 4
    Reset-All; Set-Stub $null $null
    $r.unreadable = Case "unreadable count" 4

    # 3. FOREIGN, the incident: hosted, the URL times out / errors, no host process of ours.
    Reset-All; Set-Stub 1 $null 500
    $r.foreign = Case "foreign" 6
    $r.foreignSupervisorPid = $PID
    # ... and the other PC letting go: the count falls to 0 -> re-host by itself.
    Set-Stub 0 $null
    $script:CapturedLog.Clear()
    $r.release = Case "release" 4

    # 3b. FOREIGN answering with another machine's pid.
    Reset-All; Set-Stub 2 9999
    $r.foreignPid = Case "foreign with another pid" 5

    # 4. OUR host is running (the process this supervisor launched).
    $sleeper = Start-Process -FilePath $SleeperPy -ArgumentList '-c "import time; time.sleep(300)"' -WindowStyle Hidden -PassThru
    $script:TunnelHostProc = $sleeper
    Reset-All; Set-Stub 1 $null 500
    $r.oursHostUnverified = Case "our host, tunnel unanswered" 5
    Reset-All; Set-Stub 2 9999
    $r.shared = Case "shared" 5
    $script:TunnelHostProc = $null

    # 5. THE PROBE THROUGH THE TUNNEL BACKS OFF: failures at ticks 1, 3, 6, 11 of 12.
    Reset-All; Set-Stub 1 $null 500
    $r.backoff = Case "backoff" 12
    # ... and is capped: max 4 -> probes at 1, 3, 6, 11, 16 of 20.
    Reset-All; Set-Stub 1 $null 500
    $script:TunnelProbeBackoffMax = 4
    $r.backoffCapped = Case "backoff capped" 20
    $r.backoffCapped.backoffAfter = $script:TunnelProbeBackoff
    $script:TunnelProbeBackoffMax = 40
    # ... and an answer resets it.
    Set-Stub 1 4242
    $script:TunnelProbeSkip = 0
    $r.backoffReset = Case "answered" 1
    $r.backoffReset.backoffAfter = $script:TunnelProbeBackoff
} finally {
    if ($sleeper) { Stop-Process -Id $sleeper.Id -Force -ErrorAction SilentlyContinue }
    $r | ConvertTo-Json -Depth 8 | Set-Content -Path $OutFile -Encoding UTF8
}
'''

_SUP_FUNCTIONS = (
    "function Get-HealthServerPid", "function Get-TunnelHostConnections", "function Test-TunnelHosting",
    "function Test-IsTunnelHostCommandLine", "function Test-OurTunnelHostRunning",
    "function Get-EnvTunnelUrl", "function Get-TunnelOrigin", "function Get-HealthPidAt",
    "function Resolve-TunnelHostingState", "function Reset-TunnelProbeBackoff",
    "function Get-TunnelServerPidBounded", "function Get-TunnelHostingAdvice",
    "function Set-TunnelHostStatus", "function Invoke-TunnelHostingCheck",
    "function Clear-DevtunnelLoginCache",
)
_SUP_INIT = (
    "$script:TunnelProbeSkip = 0", "$script:TunnelProbeBackoff = 1", "$script:TunnelProbeBackoffMax =",
    "$script:TunnelStatusPath =", "$script:TunnelStatusKey =", "$script:TunnelState =",
    "$script:ForeignStreak =",
)


def run_supervisor_driver(supervisor_text: str) -> dict:
    """Runs the driver built from `supervisor_text` (the real file, or a mutated copy)."""
    work = tempfile.mkdtemp(prefix="sup_tunnel_owner_")
    procs = []
    try:
        root = os.path.join(work, "repo")
        stub = os.path.join(work, "stub")
        os.makedirs(root)
        os.makedirs(stub)
        shutil.copy(NAME_UTIL_PS1, os.path.join(root, "tunnel_name_util.ps1"))
        with open(os.path.join(stub, "dt_stub.py"), "w", encoding="utf-8") as fh:
            fh.write(_DT_STUB_PY)
        with open(os.path.join(stub, "devtunnel.cmd"), "wb") as fh:
            fh.write(_DT_STUB_CMD.encode("ascii"))
        http_py = os.path.join(stub, "http_stub.py")
        with open(http_py, "w", encoding="utf-8") as fh:
            fh.write(_HTTP_STUB_PY)
        state = os.path.join(work, "state.json")
        hits = os.path.join(work, "hits.txt")
        with open(state, "w", encoding="utf-8") as fh:
            json.dump({"hosts": 0}, fh)
        open(hits, "w").close()
        local_port, tunnel_port = _free_port(), _free_port()
        with open(os.path.join(root, ".env"), "w", encoding="utf-8") as fh:
            fh.write("MCP_TUNNEL_NAME=abc-tunnel\nMCP_TUNNEL_URL=http://127.0.0.1:%d/mcp\n" % tunnel_port)
        env = dict(os.environ, SUP_STATE=state, SUP_HITS=hits, SUP_PY=sys.executable,
                   SUP_DT_LOG=os.path.join(work, "dt.log"))
        for role, port in (("local", local_port), ("tunnel", tunnel_port)):
            procs.append(subprocess.Popen([sys.executable, http_py, role, str(port)], env=env,
                                          creationflags=childproc.headless_creationflags()))
        deadline = time.time() + 20
        for port in (local_port, tunnel_port):
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    assert time.time() < deadline, "stub server on %d did not listen" % port
                    time.sleep(0.2)

        parts = [_extract_line(supervisor_text, l) for l in _SUP_INIT]
        parts += [_extract_braced_block(supervisor_text, f) for f in _SUP_FUNCTIONS]
        driver = os.path.join(work, "driver.ps1")
        out = os.path.join(work, "out.json")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(_SUP_DRIVER_HEAD + "\n\n" + "\n\n".join(parts) + "\n\n" + _SUP_DRIVER_BODY)
        proc = childproc.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver,
             "-RootDir", root, "-DtCmd", os.path.join(stub, "devtunnel.cmd"), "-StateFile", state,
             "-HitsFile", hits, "-LocalPort", str(local_port), "-SleeperPy", sys.executable,
             "-OutFile", out],
            env=env, timeout=400, creationflags=childproc.headless_creationflags())
        assert proc.returncode == 0, "driver failed:\n%s\n%s" % (proc.stdout, proc.stderr)
        with open(out, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    finally:
        for p in procs:
            try:
                p.kill()
                p.wait(10)
            except Exception:
                pass
        shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="module")
def sup():
    return run_supervisor_driver(_read(SUPERVISOR_PS1))


def _lines(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def test_ours_is_left_alone(sup):
    r = sup["ours"]
    assert set(_lines(r["states"])) == {"ours"}, r
    assert r["hostCalls"] == 0
    assert r["status"]["state"] == "ours"


@pytest.mark.parametrize("case", ["none", "unreadable"])
def test_nobody_hosting_is_rehosted_after_the_debounce_as_before(sup, case):
    r = sup[case]
    assert set(_lines(r["states"])) == {"none"}, r
    assert r["hostCalls"] == 1, "a tunnel nobody hosts was not re-hosted after four misses"
    assert any("tunnel host connections = 0 (4/4)" in l for l in _lines(r["log"])), r["log"]


def test_another_pc_serving_this_tunnel_is_reported_and_not_fought(sup):
    r = sup["foreign"]
    assert _lines(r["states"]) == ["foreign"] * 6, r["states"]
    assert r["hostCalls"] == 0, "the supervisor re-hosted a tunnel another PC is serving"
    said = [l for l in _lines(r["log"]) if l.startswith("tunnel FOREIGN")]
    assert len(said) == 1, "not said exactly once: %r" % _lines(r["log"])
    assert "Another PC is serving this PC's tunnel 'abc-tunnel'" in said[0]
    assert "quickstart.bat" in said[0] and "start_all.bat" in said[0]
    assert not [l for l in _lines(r["log"]) if "connections = 0" in l]
    st = r["status"]
    assert st["state"] == "foreign" and st["tunnel"] == "abc-tunnel"
    assert st["host_connections"] == 1 and st["this_pc_host_running"] is False
    assert st["supervisor_pid"] == sup["foreignSupervisorPid"]
    assert "OTHER PC run quickstart.bat" in st["action"] and "THIS PC run start_all.bat" in st["action"]


def test_when_the_other_pc_lets_go_this_pc_takes_its_tunnel_back(sup):
    r = sup["release"]
    assert set(_lines(r["states"])) == {"none"}
    assert any("no longer served by another PC" in l for l in _lines(r["log"])), r["log"]
    assert r["hostCalls"] == 1
    assert r["status"]["state"] == "none"


def test_a_tunnel_answering_with_another_pid_and_no_host_of_ours_is_foreign(sup):
    r = sup["foreignPid"]
    assert _lines(r["states"])[-1] == "foreign" and r["hostCalls"] == 0
    assert r["status"]["tunnel_server_pid"] == 9999 and r["status"]["local_server_pid"] == 4242


def test_our_running_host_is_ours_until_the_tunnel_names_another_pid(sup):
    r = sup["oursHostUnverified"]
    assert set(_lines(r["states"])) == {"ours"} and r["hostCalls"] == 0
    s = sup["shared"]
    assert _lines(s["states"])[-1] == "shared" and s["hostCalls"] == 0
    assert "server pid 9999" in s["status"]["message"] and "pid 4242" in s["status"]["message"]
    assert len([l for l in _lines(s["log"]) if l.startswith("tunnel SHARED")]) == 1


def test_the_probe_through_the_tunnel_backs_off_and_is_capped(sup):
    assert sup["backoff"]["hits"] == 4, "12 ticks of failures probed %d times (want 1,3,6,11)" \
        % sup["backoff"]["hits"]
    assert sup["backoffCapped"]["hits"] == 5, sup["backoffCapped"]["hits"]
    assert sup["backoffCapped"]["backoffAfter"] == 4
    assert sup["backoffReset"]["states"] in ("ours", ["ours"])
    assert sup["backoffReset"]["backoffAfter"] == 1


def test_the_main_loop_uses_the_new_check(sup):
    src = _read(SUPERVISOR_PS1)
    loop = src[src.index("\nwhile ($true) {"):]
    assert "Invoke-TunnelHostingCheck" in loop
    assert "if (Test-TunnelHosting)" not in loop, "the loop still decides on 'anybody hosts it'"


# -- THE TUNNEL'S STARTUP FAST PATH: "none" hosts at once, without the four-tick debounce -------
# Runs the REAL code between "nothing is listening on :$Port at startup" and `while ($true) {`
# (marked in supervisor.ps1 by "TUNNEL STARTUP FAST PATH (begin)"/"(end)"), not a hand-written
# stand-in -- so a change to the real startup block is what this exercises, the same way
# run_supervisor_driver exercises the real Invoke-TunnelHostingCheck.

_STARTUP_FUNCTIONS = (
    "function Get-HealthServerPid", "function Get-TunnelHostConnections",
    "function Test-IsTunnelHostCommandLine", "function Test-OurTunnelHostRunning",
    "function Get-EnvTunnelUrl", "function Get-TunnelOrigin", "function Get-HealthPidAt",
    "function Resolve-TunnelHostingState", "function Reset-TunnelProbeBackoff",
    "function Get-TunnelServerPidBounded", "function Get-TunnelHostingAdvice",
    "function Set-TunnelHostStatus", "function Clear-DevtunnelLoginCache",
)
_STARTUP_INIT = (
    "$script:TunnelProbeSkip = 0", "$script:TunnelProbeBackoff = 1", "$script:TunnelProbeBackoffMax =",
    "$script:TunnelStatusPath =", "$script:TunnelStatusKey =", "$script:TunnelState =",
    "$script:ForeignStreak =",
)

_STARTUP_DRIVER_HEAD = r'''param(
    [Parameter(Mandatory=$true)][string]$RootDir,
    [Parameter(Mandatory=$true)][string]$DtCmd,
    [Parameter(Mandatory=$true)][string]$StateFile,
    [Parameter(Mandatory=$true)][int]$LocalPort,
    [Parameter(Mandatory=$true)][string]$OutFile,
    [string]$LoggedInStr = "true"
)
$ErrorActionPreference = "SilentlyContinue"
$script:CapturedLog = New-Object System.Collections.Generic.List[string]
function Write-Log($msg) { $script:CapturedLog.Add([string]$msg) }
$Root = $RootDir
$Port = $LocalPort
$DevTunnel = $DtCmd
$TunnelName = "abc-tunnel"
$script:TunnelHostProc = $null
$script:HostCalls = 0
# NOT $LoggedIn / $loggedIn: PowerShell variable names are case-insensitive, and the real
# startup block sets "$loggedIn = $null" right after this head runs -- a same-named stub
# variable would be overwritten by that before Test-DevtunnelLoggedInCached ever reads it.
$script:StubDevtunnelLoggedIn = ($LoggedInStr -eq "true")
function Test-DevtunnelLoggedInCached { return $script:StubDevtunnelLoggedIn }
# RECORDS instead of launching a real `devtunnel host`: what is under test is whether the
# startup block DECIDES to host at once, not devtunnel itself.
function Start-TunnelHost { $script:HostCalls++; return $true }
. (Join-Path $RootDir "tunnel_name_util.ps1")
'''

_STARTUP_DRIVER_TAIL = r'''
$r = [ordered]@{
    hostCalls  = $script:HostCalls
    log        = @($script:CapturedLog)
    tunnelState = $script:TunnelState
    loggedInVar = $loggedIn
    status     = $(if (Test-Path $script:TunnelStatusPath) { Get-Content $script:TunnelStatusPath -Raw | ConvertFrom-Json } else { $null })
}
$r | ConvertTo-Json -Depth 8 | Set-Content -Path $OutFile -Encoding UTF8
'''


def run_startup_driver(supervisor_text: str, hosts, tunnel_pid, tunnel_status=200, logged_in=True):
    """Runs the REAL startup fast-path block (extracted between its begin/end markers) against
    the same stub devtunnel/HTTP servers run_supervisor_driver uses, with one state set before
    the block runs once -- there is no loop here, only the single startup pass."""
    work = tempfile.mkdtemp(prefix="sup_tunnel_startup_")
    procs = []
    try:
        root = os.path.join(work, "repo")
        stub = os.path.join(work, "stub")
        os.makedirs(root)
        os.makedirs(stub)
        shutil.copy(NAME_UTIL_PS1, os.path.join(root, "tunnel_name_util.ps1"))
        with open(os.path.join(stub, "dt_stub.py"), "w", encoding="utf-8") as fh:
            fh.write(_DT_STUB_PY)
        with open(os.path.join(stub, "devtunnel.cmd"), "wb") as fh:
            fh.write(_DT_STUB_CMD.encode("ascii"))
        http_py = os.path.join(stub, "http_stub.py")
        with open(http_py, "w", encoding="utf-8") as fh:
            fh.write(_HTTP_STUB_PY)
        state = os.path.join(work, "state.json")
        hits = os.path.join(work, "hits.txt")
        with open(state, "w", encoding="utf-8") as fh:
            json.dump({"hosts": hosts, "tunnel_pid": tunnel_pid, "tunnel_status": tunnel_status}, fh)
        open(hits, "w").close()
        local_port, tunnel_port = _free_port(), _free_port()
        with open(os.path.join(root, ".env"), "w", encoding="utf-8") as fh:
            fh.write("MCP_TUNNEL_NAME=abc-tunnel\nMCP_TUNNEL_URL=http://127.0.0.1:%d/mcp\n" % tunnel_port)
        env = dict(os.environ, SUP_STATE=state, SUP_HITS=hits, SUP_PY=sys.executable,
                   SUP_DT_LOG=os.path.join(work, "dt.log"))
        for role, port in (("local", local_port), ("tunnel", tunnel_port)):
            procs.append(subprocess.Popen([sys.executable, http_py, role, str(port)], env=env,
                                          creationflags=childproc.headless_creationflags()))
        deadline = time.time() + 20
        for port in (local_port, tunnel_port):
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    assert time.time() < deadline, "stub server on %d did not listen" % port
                    time.sleep(0.2)

        parts = [_extract_line(supervisor_text, l) for l in _STARTUP_INIT]
        parts += [_extract_braced_block(supervisor_text, f) for f in _STARTUP_FUNCTIONS]
        parts.append(_extract_between(supervisor_text, "TUNNEL STARTUP FAST PATH (begin)",
                                      "TUNNEL STARTUP FAST PATH (end)"))
        # $loggedIn: the startup block sets it to $false on the not-logged-in branch; declared
        # here (as the real file does at "$loggedIn = $null" just above the block) so that
        # assignment lands somewhere real instead of silently creating a new local.
        parts.insert(0, "$loggedIn = $null")
        driver = os.path.join(work, "driver.ps1")
        out = os.path.join(work, "out.json")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(_STARTUP_DRIVER_HEAD + "\n\n" + "\n\n".join(parts) + "\n\n" + _STARTUP_DRIVER_TAIL)
        proc = childproc.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver,
             "-RootDir", root, "-DtCmd", os.path.join(stub, "devtunnel.cmd"), "-StateFile", state,
             "-LocalPort", str(local_port), "-OutFile", out,
             "-LoggedInStr", ("true" if logged_in else "false")],
            env=env, timeout=120, creationflags=childproc.headless_creationflags())
        assert proc.returncode == 0, "driver failed:\n%s\n%s" % (proc.stdout, proc.stderr)
        with open(out, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    finally:
        for p in procs:
            try:
                p.kill()
                p.wait(10)
            except Exception:
                pass
        shutil.rmtree(work, ignore_errors=True)


def test_startup_hosts_at_once_when_nobody_hosts_it_no_debounce_wait():
    r = run_startup_driver(_read(SUPERVISOR_PS1), hosts=0, tunnel_pid=None)
    assert r["hostCalls"] == 1, "a tunnel nobody hosts at startup was not hosted at once"
    assert r["tunnelState"] == "none"
    # THE BEHAVIORAL DIFFERENCE FROM THE TICK LOOP: no "tunnel host connections = 0 (n/4)"
    # miss-counting happened first -- this fired on the very first look, not the fourth.
    assert not [l for l in r["log"] if "connections = 0 (" in l], r["log"]
    assert any("at startup -> hosting the tunnel now, without the debounce" in l for l in r["log"]), r["log"]
    assert r["status"]["state"] == "none"


def test_startup_leaves_an_already_ours_tunnel_alone():
    r = run_startup_driver(_read(SUPERVISOR_PS1), hosts=1, tunnel_pid=4242)
    assert r["hostCalls"] == 0
    assert r["tunnelState"] == "ours"
    assert r["status"]["state"] == "ours"


def test_startup_never_fights_a_tunnel_another_pc_is_serving():
    r = run_startup_driver(_read(SUPERVISOR_PS1), hosts=1, tunnel_pid=None, tunnel_status=500)
    assert r["hostCalls"] == 0, "the startup fast path fought a foreign tunnel"
    assert r["tunnelState"] == "foreign"
    assert r["status"]["state"] == "foreign"
    # foreign/shared are reported only after $FailuresBeforeAction misses in the tick loop; the
    # startup block itself must not have printed its own "tunnel FOREIGN" line early.
    assert not [l for l in r["log"] if l.startswith("tunnel FOREIGN")], r["log"]


def test_startup_does_not_touch_devtunnel_when_not_logged_in():
    r = run_startup_driver(_read(SUPERVISOR_PS1), hosts=0, tunnel_pid=None, logged_in=False)
    assert r["hostCalls"] == 0, "devtunnel was touched while not logged in"
    assert r["loggedInVar"] is False


# -- doctor.ps1 reads the supervisor's verdict, but only a live supervisor's ---------------------

def test_doctor_trusts_the_status_file_only_while_its_supervisor_lives(tmp_path):
    doctor = _read(os.path.join(REPO, "scripts", "doctor.ps1"))
    fn = _extract_braced_block(doctor, "function Get-SupervisorTunnelVerdict")
    (tmp_path / ".fleet").mkdir()
    status = tmp_path / ".fleet" / "tunnel_host.json"
    # A live process whose command line names supervisor.ps1 (as the real one's does), and a
    # live one that does not.
    fake_sup = subprocess.Popen([_POWERSHELL, "-NoProfile", "-Command",
                                 "Start-Sleep 120 # scripts\\supervisor.ps1"],
                                creationflags=childproc.headless_creationflags())
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             creationflags=childproc.headless_creationflags())
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(30)
    try:
        time.sleep(1.0)
        drv = tmp_path / "d.ps1"
        out = tmp_path / "o.json"
        lines = ["$ErrorActionPreference = 'Continue'", "$repo = '%s'" % tmp_path, fn,
                 "$r = [ordered]@{}"]
        for label, pid in (("live", fake_sup.pid), ("notSupervisor", other.pid), ("dead", dead.pid)):
            doc = json.dumps({"state": "foreign", "message": "m", "action": "a", "supervisor_pid": pid})
            lines.append("[IO.File]::WriteAllText('%s', '%s')" % (status, doc))
            lines.append("$v = Get-SupervisorTunnelVerdict; $r['%s'] = $(if ($v) { $v.state } else { 'none' })"
                         % label)
        lines.append("$r | ConvertTo-Json | Set-Content -Path '%s' -Encoding UTF8" % out)
        drv.write_text("\n".join(lines), encoding="utf-8")
        proc = childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(drv)],
                             timeout=90, creationflags=childproc.headless_creationflags())
        assert proc.returncode == 0, proc.stderr
        got = json.loads(out.read_text(encoding="utf-8-sig"))
    finally:
        for p in (fake_sup, other):
            p.kill()
    assert got == {"live": "foreign", "notSupervisor": "none", "dead": "none"}, got


# -- setup_devtunnel.ps1: never adopt a tunnel another machine is hosting ------------------------

_SETUP_STUB = setup_rig._STUB_PY
_SHOW_OLD = '    print("Tunnel ID             : %s.jpe1" % n)\n'
_HOST_OLD = 'if a[:1] == ["host"]:\n    sys.exit(0)\n'
assert _SETUP_STUB.count(_SHOW_OLD) == 1 and _SETUP_STUB.count(_HOST_OLD) == 1
_SETUP_STUB = _SETUP_STUB.replace(
    _SHOW_OLD, _SHOW_OLD + '    print("Host connections      : %d" % owned[n].get("hosts", 0))\n')
_SETUP_STUB = _SETUP_STUB.replace(
    _HOST_OLD, 'if a[:1] == ["host"]:\n    import time; time.sleep(st.get("host_sleep", 0))\n    sys.exit(0)\n')


@pytest.fixture()
def rig_cls(monkeypatch):
    monkeypatch.setattr(setup_rig, "_STUB_PY", _SETUP_STUB)
    return setup_rig.Rig


def _tunnel(name, hosts):
    return {"tunnel": ["+Anonymous [connect]"], "ports": {"8000": ["+Anonymous [connect]"]},
            "hosts": hosts}


def _mine():
    return "m365-copilot-companion-" + setup_rig._this_suffix()


def _touched(calls, name):
    return [c for c in calls if name in c and c[:1] in (["host"], ["port"], ["access"])]


def test_a_carried_name_hosted_by_another_machine_is_not_adopted(tmp_path, rig_cls):
    rig = rig_cls(tmp_path, "MCP_API_KEY=k\nMCP_TUNNEL_NAME=teamtunnel\n",
                  {"owned": {"teamtunnel": _tunnel("teamtunnel", 1)}}).run("-ForceAnonymous")
    assert rig.rc == 0, rig.out
    assert rig.live("MCP_TUNNEL_NAME") == _mine(), rig.env_lines
    assert ["create", _mine(), "--allow-anonymous"] in rig.calls
    assert not _touched(rig.calls, "teamtunnel"), "the other machine's tunnel was touched"
    assert "ANOTHER machine is hosting it right now (the name came from .env (MCP_TUNNEL_NAME))" in rig.out


def test_an_explicit_name_hosted_by_another_machine_is_not_adopted(tmp_path, rig_cls):
    rig = rig_cls(tmp_path, "MCP_API_KEY=k\n",
                  {"owned": {"teamtunnel": _tunnel("teamtunnel", 2)}}).run("-TunnelName", "teamtunnel",
                                                                           "-ForceAnonymous")
    assert rig.rc == 0, rig.out
    assert rig.live("MCP_TUNNEL_NAME") == _mine()
    assert "(the name came from the -TunnelName parameter)" in rig.out


def test_the_bare_legacy_default_hosted_elsewhere_is_not_adopted(tmp_path, rig_cls):
    rig = rig_cls(tmp_path, "MCP_API_KEY=k\n",
                  {"owned": {"m365-copilot-companion": _tunnel("m365-copilot-companion", 1)}}
                  ).run("-ForceAnonymous")
    assert rig.rc == 0, rig.out
    assert rig.live("MCP_TUNNEL_NAME") == _mine()
    assert not _touched(rig.calls, "m365-copilot-companion")


def test_an_unhosted_carried_name_is_still_reused(tmp_path, rig_cls):
    rig = rig_cls(tmp_path, "MCP_API_KEY=k\nMCP_TUNNEL_NAME=teamtunnel\n",
                  {"owned": {"teamtunnel": _tunnel("teamtunnel", 0)}}).run("-ForceAnonymous")
    assert rig.rc == 0, rig.out
    assert rig.live("MCP_TUNNEL_NAME") == "teamtunnel"
    assert not [c for c in rig.calls if c[:1] == ["create"]]


def test_this_machines_own_default_is_kept_even_while_hosted(tmp_path, rig_cls):
    rig = rig_cls(tmp_path, "MCP_API_KEY=k\nMCP_TUNNEL_NAME=%s\n" % _mine(),
                  {"owned": {_mine(): _tunnel(_mine(), 1)}}).run("-ForceAnonymous")
    assert rig.rc == 0, rig.out
    assert rig.live("MCP_TUNNEL_NAME") == _mine()
    assert "ANOTHER machine" not in rig.out


def test_a_tunnel_this_machine_is_hosting_is_reused(tmp_path, rig_cls):
    """The host count is this machine's own `devtunnel host teamtunnel` (re-running quickstart
    on the PC that serves it) -- a live process whose command line hosts it."""
    state = {"owned": {"teamtunnel": _tunnel("teamtunnel", 1)}, "host_sleep": 120}
    rig = rig_cls(tmp_path, "MCP_API_KEY=k\nMCP_TUNNEL_NAME=teamtunnel\n", state)
    host = subprocess.Popen(["cmd.exe", "/c", str(rig.stub / "devtunnel.cmd"), "host", "teamtunnel"],
                            env=rig.env, creationflags=childproc.headless_creationflags())
    try:
        time.sleep(1.5)
        rig.run("-ForceAnonymous")
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(host.pid)], capture_output=True)
    assert rig.rc == 0, rig.out
    assert rig.live("MCP_TUNNEL_NAME") == "teamtunnel", rig.out
    assert "ANOTHER machine" not in rig.out
