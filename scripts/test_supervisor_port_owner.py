# -*- coding: utf-8 -*-
r"""The supervisor stops only THIS checkout's server, and only this server's answer counts (D13).

THE DEFECT. scripts/supervisor.ps1's Start-Server stopped EVERY process listening on :8000, and
Test-ServerUp counted ANY HTTP answer there -- a 404, a directory listing -- as the server being
up. Another local service on the port was either killed about once a minute, or taken for the
server so that main.py was never started while doctor said the server was down.

THE FIX. Get-ProcessVerdict: a port holder is ours only when its own or its parent's command
line is this checkout's main.py (the .venv python.exe is a launcher; the port is owned by the
base interpreter it starts, measured), or it is the process this supervisor launched.
Test-ServerUp: a 200 counts only when it is main.py's /health JSON with a server_pid that is
ours; an HTTP error counts only when the port's owner is ours. Start-Server: when every holder is
foreign, nothing is stopped or launched and the log says who holds the port and what to do.
Get-PortListenerPids also fixes the empty-port case: Get-NetTCPConnection THROWS ObjectNotFound
when nothing listens, which used to read as "could not look".

HOW THIS RUNS. Extracted functions in a temp driver (see test_a_silent_death_leaves_its_exit_code
for why never dot-source supervisor.ps1), on a FREE port -- never 8000 -- with a temp folder as
$Root, against real processes: a fake main.py in that folder (answers /health like main.py), the
same file in a sibling folder whose name only starts with the root's ("another checkout"), a
plain http.server (404 on /health), a server answering 200 "hello", and a raw TCP listener that
never answers. Start-Server really runs: it relaunches the fake main.py from the temp root and,
first, moves $Py from the base interpreter onto a throwaway .venv (D2's launch site).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

SUPERVISOR_PS1 = os.path.join(REPO, "scripts", "supervisor.ps1")

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason="scripts/supervisor.ps1 is Windows/PowerShell-only (os.name=%r, powershell found=%r)"
           % (os.name, bool(_POWERSHELL)),
)


def _extract_braced_block(text: str, start_marker: str) -> str:
    """Balanced-brace block at `start_marker` (not PowerShell-aware; every brace inside a string
    literal in the extracted functions is itself balanced -- checked by hand)."""
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
    raise AssertionError("unbalanced braces extracting block at %r" % (start_marker,))


def _extract_line(text: str, needle: str) -> str:
    idx = text.index(needle)
    return text[text.rfind("\n", 0, idx) + 1:text.index("\n", idx)]


def _code_only(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


@pytest.fixture(scope="module")
def src() -> str:
    with open(SUPERVISOR_PS1, "r", encoding="utf-8") as fh:
        return fh.read()


def _base_python() -> str:
    return getattr(sys, "_base_executable", None) or sys.executable


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert port != 8000
    return port


_FAKE_MAIN = r'''
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
PORT = int(os.environ["FAKE_MAIN_PORT"])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "server_pid": os.getpid(), "server_code": "current"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", PORT), H).serve_forever()
'''

_FOREIGN_200 = r'''
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "5")
        self.end_headers()
        self.wfile.write(b"hello")
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", int(os.environ["FAKE_MAIN_PORT"])), H).serve_forever()
'''

_FOREIGN_TCP = r'''
import os, socket, time
s = socket.socket()
s.bind(("127.0.0.1", int(os.environ["FAKE_MAIN_PORT"])))
s.listen(5)
time.sleep(600)
'''

_FOREIGN_404 = r'''
import os
from http.server import SimpleHTTPRequestHandler, HTTPServer
class H(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", int(os.environ["FAKE_MAIN_PORT"])), H).serve_forever()
'''


# -- pure pieces ----------------------------------------------------------------------------

_PURE_DRIVER = r'''param([Parameter(Mandatory=$true)][string]$OutFile)
$ErrorActionPreference = "Stop"
%s
$root = 'C:\work\repo'
$r = [ordered]@{
    venvLauncher = Test-IsThisCheckoutServerCommandLine '"C:\work\repo\.venv\Scripts\python.exe" main.py' $root
    rootMain     = Test-IsThisCheckoutServerCommandLine 'C:\py\python.exe "C:\work\repo\main.py"' $root
    siblingRepo  = Test-IsThisCheckoutServerCommandLine '"C:\work\repo2\.venv\Scripts\python.exe" main.py' $root
    testMain     = Test-IsThisCheckoutServerCommandLine '"C:\work\repo\.venv\Scripts\python.exe" -m pytest tests\test_main.py' $root
    noRoot       = Test-IsThisCheckoutServerCommandLine 'C:\py\python.exe main.py' $root
    bareMain     = Test-IsMainPyCommandLine 'C:\py\python.exe main.py '
    healthOk     = Get-HealthServerPid '{"status": "ok", "server_pid": 4242, "server_code": "current"}'
    healthNoPid  = Get-HealthServerPid '{"status": "ok"}'
    healthHtml   = Get-HealthServerPid '<html>hello</html>'
    healthBadSt  = Get-HealthServerPid '{"status": "down", "server_pid": 4242}'
}
$r | ConvertTo-Json | Set-Content -Path $OutFile -Encoding UTF8
'''


def test_identity_rules_on_command_lines_and_health_bodies(src, tmp_path):
    funcs = "\n\n".join(_extract_braced_block(src, m) for m in (
        "function Test-IsMainPyCommandLine", "function Test-IsThisCheckoutServerCommandLine",
        "function Get-HealthServerPid"))
    driver = tmp_path / "pure.ps1"
    out = tmp_path / "pure.json"
    driver.write_text(_PURE_DRIVER % funcs, encoding="utf-8")
    proc = childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(driver), "-OutFile", str(out)], timeout=60,
                         creationflags=childproc.headless_creationflags())
    assert proc.returncode == 0, proc.stderr
    r = json.loads(out.read_text(encoding="utf-8-sig"))
    assert r["venvLauncher"] is True and r["rootMain"] is True
    assert r["siblingRepo"] is False, "C:\\work\\repo claimed C:\\work\\repo2's server"
    assert r["testMain"] is False, "a pytest run of test_main.py counted as the server"
    assert r["noRoot"] is False and r["bareMain"] is True
    assert r["healthOk"] == 4242
    assert r["healthNoPid"] is None and r["healthHtml"] is None and r["healthBadSt"] is None


# -- real processes on a free port ----------------------------------------------------------

_DRIVER_HEADER = r'''param(
    [Parameter(Mandatory=$true)][string]$RootDir,
    [Parameter(Mandatory=$true)][string]$SiblingDir,
    [Parameter(Mandatory=$true)][string]$BasePy,
    [Parameter(Mandatory=$true)][string]$VenvPython,
    [Parameter(Mandatory=$true)][string]$ForeignDir,
    [Parameter(Mandatory=$true)][int]$TestPort,
    [Parameter(Mandatory=$true)][string]$OutFile
)
# THE SUPERVISOR'S OWN SETTING, so the extracted code runs as it does there.
$ErrorActionPreference = "SilentlyContinue"
$script:CapturedLog = New-Object System.Collections.Generic.List[string]
function Write-Log($msg) { $script:CapturedLog.Add([string]$msg) }
$Root = $RootDir
$Port = $TestPort
$script:Py = $BasePy
$script:VenvPy = $VenvPython
$script:ServerProc = $null
$script:ServerPlannedEndReason = $null
$script:LastLaunchAt = $null
$env:FAKE_MAIN_PORT = [string]$TestPort
'''

_DRIVER_FOOTER = r'''
function Wait-Listening([int]$Seconds = 20) {
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        $c = New-Object System.Net.Sockets.TcpClient
        try { $c.Connect("127.0.0.1", $Port); $c.Close(); return $true } catch { } finally { $c.Dispose() }
        Start-Sleep -Milliseconds 200
    }
    return $false
}
function Wait-ServerUp([int]$Seconds = 30) {
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        if (Test-ServerUp) { return $true }
        Start-Sleep -Milliseconds 300
    }
    return $false
}
function Start-Holder([string]$Script) {
    $p = Start-Process -FilePath $BasePy -ArgumentList ('"' + $Script + '"') -WindowStyle Hidden -PassThru
    if (-not (Wait-Listening)) { throw "holder did not listen: $Script" }
    return $p
}
function Alive($p) { try { $p.Refresh(); return (-not $p.HasExited) } catch { return $false } }
function Stop-Tree($p) {
    if (-not $p) { return }
    Get-CimInstance Win32_Process -Filter ("ParentProcessId=" + $p.Id) -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    try { $p.WaitForExit(10000) | Out-Null } catch { }
}
function Wait-PortFree([int]$Seconds = 15) {
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        if (@((Get-PortListenerPids).Pids).Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 200
    }
    return $false
}

$r = [ordered]@{}
$spawned = New-Object System.Collections.Generic.List[object]
try {
    $empty = Get-PortListenerPids
    $r.emptyPort = [ordered]@{ queried = $empty.Queried; count = @($empty.Pids).Count }

    $cases = @(
        @{ name = "404"; script = (Join-Path $ForeignDir "foreign_404.py") },
        @{ name = "hello"; script = (Join-Path $ForeignDir "foreign_200.py") },
        @{ name = "sibling"; script = (Join-Path $SiblingDir "main.py") },
        @{ name = "tcp"; script = (Join-Path $ForeignDir "foreign_tcp.py") }
    )
    foreach ($c in $cases) {
        $script:CapturedLog.Clear()
        $script:ForeignHolderKey = ""
        $h = Start-Holder $c.script
        $spawned.Add($h)
        $up = Test-ServerUp
        $check = $script:LastServerCheck
        Start-Server
        $logAfterFirst = @($script:CapturedLog)
        Start-Server
        $r[$c.name] = [ordered]@{
            up = $up; check = $check; holderPid = $h.Id
            holderAliveAfter = (Alive $h)
            launched = [bool]$script:ServerProc
            logFirst = $logAfterFirst
            logAll = @($script:CapturedLog)
        }
        Stop-Tree $h
        Wait-PortFree | Out-Null
    }

    # OURS, started by hand from the checkout (command line names <root>\main.py). The refused
    # launches above already re-decided the interpreter; start this one on the PATH python again.
    $r.pyAfterRefusals = $script:Py
    $script:Py = $BasePy
    $script:CapturedLog.Clear()
    $mine = Start-Holder (Join-Path $RootDir "main.py")
    $spawned.Add($mine)
    $r.ours = [ordered]@{ up = (Test-ServerUp); check = $script:LastServerCheck; pid = $mine.Id }

    # Start-Server replaces it -- and first moves $Py onto the .venv (D2's launch site).
    Start-Server
    $spawned.Add($script:ServerProc)
    $r.relaunchVenv = [ordered]@{
        oldAlive = (Alive $mine)
        py = $script:Py
        launchedPath = $script:ServerProc.Path
        up = (Wait-ServerUp)
        check = $script:LastServerCheck
        log = @($script:CapturedLog)
    }
    # The port owner is the base interpreter the .venv launcher started; with no tracked
    # process, only the parent's command line can say it is ours.
    $venvLaunch = $script:ServerProc
    $script:ServerProc = $null
    $script:VerifiedServerPid = 0
    $r.parentRule = [ordered]@{ up = (Test-ServerUp); check = $script:LastServerCheck }
    $script:ServerProc = $venvLaunch

    # A launch on a PATH python names no checkout: only the tracked process makes it ours.
    $script:Py = $BasePy
    $script:VenvPy = (Join-Path $RootDir "no-venv\python.exe")
    Start-Server
    $spawned.Add($script:ServerProc)
    $baseLaunch = $script:ServerProc
    $r.relaunchBase = [ordered]@{
        venvLaunchAlive = (Alive $venvLaunch)
        up = (Wait-ServerUp)
        check = $script:LastServerCheck
    }
    Start-Server
    $spawned.Add($script:ServerProc)
    $r.relaunchAgain = [ordered]@{
        baseLaunchAlive = (Alive $baseLaunch)
        up = (Wait-ServerUp)
    }
} finally {
    foreach ($p in $spawned) { Stop-Tree $p }
    $r | ConvertTo-Json -Depth 8 | Set-Content -Path $OutFile -Encoding UTF8
}
'''

_FUNCTIONS = (
    "function Test-PythonRuns", "function Update-PythonInterpreter",
    "function Get-PortListenerPids", "function Test-IsMainPyCommandLine",
    "function Test-IsThisCheckoutServerCommandLine", "function Get-ProcessVerdict",
    "function Get-HealthServerPid", "function Write-ForeignPortHolderLog",
    "function Test-ServerUp", "function Get-ServerExitRecord", "function Start-Server",
)
_INIT_LINES = (
    "$script:PyRecheckSeconds =", "$script:PyRejectedAt = $null", "$script:PyRejectedWhy = $null",
    "$script:ForeignHolderKey =", "$script:ForeignHolderLoggedAt =", "$script:VerifiedServerPid =",
)


@pytest.fixture(scope="module")
def driver_result(src):
    work = tempfile.mkdtemp(prefix="sup_port_owner_")
    try:
        root = os.path.join(work, "repo")
        sibling = os.path.join(work, "repo2")
        foreign = os.path.join(work, "other")
        for d in (root, sibling, foreign):
            os.makedirs(d)
        for d in (root, sibling):
            with open(os.path.join(d, "main.py"), "w", encoding="ascii") as fh:
                fh.write(_FAKE_MAIN)
        for name, body in (("foreign_404.py", _FOREIGN_404), ("foreign_200.py", _FOREIGN_200),
                           ("foreign_tcp.py", _FOREIGN_TCP)):
            with open(os.path.join(foreign, name), "w", encoding="ascii") as fh:
                fh.write(body)
        proc = childproc.run([_base_python(), "-m", "venv", "--without-pip",
                              os.path.join(root, ".venv")], timeout=180,
                             creationflags=childproc.headless_creationflags())
        assert proc.returncode == 0, "venv creation failed:\n%s\n%s" % (proc.stdout, proc.stderr)
        venv_py = os.path.join(root, ".venv", "Scripts", "python.exe")

        parts = [_extract_line(src, l) for l in _INIT_LINES]
        parts += [_extract_braced_block(src, f) for f in _FUNCTIONS]
        driver = os.path.join(work, "driver.ps1")
        out = os.path.join(work, "out.json")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(_DRIVER_HEADER + "\n\n" + "\n\n".join(parts) + "\n\n" + _DRIVER_FOOTER)
        proc = childproc.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver,
             "-RootDir", root, "-SiblingDir", sibling, "-BasePy", _base_python(),
             "-VenvPython", venv_py, "-ForeignDir", foreign, "-TestPort", str(_free_port()),
             "-OutFile", out],
            timeout=400, creationflags=childproc.headless_creationflags())
        assert proc.returncode == 0, "driver failed:\n%s\n%s" % (proc.stdout, proc.stderr)
        with open(out, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        data["_paths"] = {"root": root, "venv": venv_py, "base": _base_python()}
        return data
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _lines(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def test_an_empty_port_is_an_answer_not_a_failed_query(driver_result):
    assert driver_result["emptyPort"] == {"queried": True, "count": 0}, \
        "nothing listening still reads as 'could not inspect the port'"


@pytest.mark.parametrize("case,expect", [
    ("404", "foreign: HTTP 404"),
    ("hello", "not main.py's answer"),
    ("sibling", "not this checkout's server"),
    ("tcp", ""),
])
def test_a_foreign_holder_is_neither_taken_for_the_server_nor_killed(driver_result, case, expect):
    r = driver_result[case]
    assert r["up"] is False, "%s on the port was taken for the server (%s)" % (case, r["check"])
    if expect:
        assert expect in r["check"], r["check"]
    assert r["holderAliveAfter"] is True, "Start-Server killed a process that is not ours (%s)" % case
    assert r["launched"] is False, "Start-Server launched main.py into a port it cannot bind"
    first = [l for l in _lines(r["logFirst"]) if "will NOT stop it" in l]
    assert len(first) == 1, _lines(r["logFirst"])
    assert "pid %d" % r["holderPid"] in first[0], "the log does not name the holder"
    assert "To fix:" in first[0], "the log does not say what to do"
    assert sum("will NOT stop it" in l for l in _lines(r["logAll"])) == 1, \
        "the same holder was reported again on the next refused launch"


def test_this_checkouts_server_is_recognised_replaced_and_moved_onto_the_venv(driver_result):
    assert driver_result["ours"]["up"] is True, driver_result["ours"]["check"]
    rv = driver_result["relaunchVenv"]
    assert rv["oldAlive"] is False, "Start-Server did not stop this checkout's own server"
    assert rv["py"] == driver_result["_paths"]["venv"], "the launch still used the PATH python"
    assert (rv["launchedPath"] or "").lower() == driver_result["_paths"]["venv"].lower()
    assert any("switching" in l for l in _lines(rv["log"])), _lines(rv["log"])
    assert rv["up"] is True, rv["check"]
    assert driver_result["parentRule"]["up"] is True, \
        "the base-interpreter child of the .venv launcher was not recognised: %s" \
        % driver_result["parentRule"]["check"]


def test_a_server_launched_on_a_path_python_is_still_ours(driver_result):
    rb = driver_result["relaunchBase"]
    assert rb["venvLaunchAlive"] is False
    assert rb["up"] is True, "a server this supervisor launched was taken for a stranger: %s" % rb["check"]
    ra = driver_result["relaunchAgain"]
    assert ra["baseLaunchAlive"] is False, "the PATH-python server was not stopped on relaunch"
    assert ra["up"] is True


def test_the_main_loop_keeps_the_same_identity_rule_for_a_booting_server(src):
    loop = _code_only(src[src.index("\nwhile ($true) {"):])
    booting = loop[loop.index("$booting = $false"):loop.index("if ($booting)")]
    assert "Test-IsThisCheckoutServerCommandLine" in booting
    assert "$script:ServerProc" in booting
