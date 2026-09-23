# -*- coding: utf-8 -*-
r"""The supervisor's fleet resume: never a second coordinator, never a reap under a resume.

TWO RULES, both from the start_all work in 1f4588a (new-PC D14):

1. RESUME ONLY WHEN NOTHING IS RUNNING. The marker .fleet/fleet_run_active.json names the pid of
   the run that wrote it, and a run resumed a moment ago (by start_all's
   resume_interrupted_fleet.py, or by hand) writes its fresh marker only after its imports and
   ledger load -- so for that window the marker still names a DEAD pid while a coordinator is
   alive. start_all now skips its resume when a relay.fleet_runner of this checkout is running;
   supervisor.ps1's startup resume did not, and put a second coordinator on the same .fleet.
   Get-ThisCheckoutFleetCoordinatorPids is MIRRORED from start_all.ps1 (sharing it means
   changing start_all.ps1, not this change's file); the first test here fails if they differ.

2. THE STALE-RUN REAP MUST NOT DELETE THE MARKER OF A RUN BEING RESUMED. Invoke-FleetReap runs
   on the first tick right after the startup resume (and on every tick and express pass after),
   and the reaper deletes a marker whose pid is dead -- the resumed run's predecessor's, before
   the resumed run has replaced it. A GUARD, not an ordering (see Get-FleetReapHoldReason): the
   resume has to come first, and the window it opens is longer than a tick.

HOW THIS RUNS. Extracted functions in a temp driver (never dot-source supervisor.ps1: forever
loop, machine-wide mutex, a live supervisor on this machine), with a temp folder as $Root. That
folder carries its own relay/ package: fleet_reaper.py is the REAL module copied in, and
fleet_runner.py is a stand-in that waits for a GO file before writing its own marker and for a
STOP file before exiting -- so "resumed, marker not yet rewritten" is held open on purpose, not
raced. tools/notify_ops.py is a stub, so the resume's desktop notification cannot reach a real
desktop. Nothing touches this checkout's .fleet.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

SUPERVISOR_PS1 = os.path.join(REPO, "scripts", "supervisor.ps1")
START_ALL_PS1 = os.path.join(REPO, "scripts", "start_all.ps1")

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

windows_only = pytest.mark.skipif(
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


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def src() -> str:
    return _read(SUPERVISOR_PS1)


# -- every platform: the mirror has not drifted ----------------------------------------------

def test_the_coordinator_detection_is_the_same_as_start_alls():
    """A copy is only safe while it IS a copy. Whitespace-normalised text equality: if start_all
    changes how it recognises a running coordinator, this fails until the supervisor agrees."""
    norm = lambda s: re.sub(r"\s+", " ", _code_only(s)).strip()
    mine = _extract_braced_block(_read(SUPERVISOR_PS1), "function Get-ThisCheckoutFleetCoordinatorPids")
    theirs = _extract_braced_block(_read(START_ALL_PS1), "function Get-ThisCheckoutFleetCoordinatorPids")
    assert norm(mine) == norm(theirs), "supervisor.ps1 and start_all.ps1 detect coordinators differently"


def test_the_startup_resume_still_comes_before_the_first_reap(src):
    code = _code_only(src)
    assert code.index("Invoke-FleetAutoResume -DryRun:$FleetResumeDryRun") < \
        code.index("\nwhile ($true) {"), "the startup resume moved into or after the loop"
    reap = _code_only(_extract_braced_block(src, "function Invoke-FleetReap"))
    assert reap.index("Get-FleetReapHoldReason") < reap.index("reap_stale_run"), \
        "the reap runs before asking whether a resume is in progress"
    assert "reap_stale_run(r'$FleetDir')" in reap, "the reaper is left to the working directory"


# -- Windows: real processes against a temp checkout ------------------------------------------

_FAKE_RUNNER = r'''
import json, os, sys, time
d = os.path.join(os.getcwd(), ".fleet")
while not os.path.exists(os.path.join(d, "GO")):
    if os.path.exists(os.path.join(d, "STOP")):
        sys.exit(3)          # died before writing its own marker
    time.sleep(0.1)
tmp = os.path.join(d, "fleet_run_active.json.tmp")
with open(tmp, "w") as fh:
    json.dump({"pid": os.getpid(), "start_ts": time.time(), "argv": [], "resume_argv": ["--x"]}, fh)
os.replace(tmp, os.path.join(d, "fleet_run_active.json"))
while not os.path.exists(os.path.join(d, "STOP")):
    time.sleep(0.1)
'''

_FAKE_NOTIFY = r'''
import os
def notify_desktop(title, body):
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".fleet", "NOTIFIED"), "w") as fh:
        fh.write(title)
'''

_DRIVER_HEADER = r'''param(
    [Parameter(Mandatory=$true)][string]$RootDir,
    [Parameter(Mandatory=$true)][string]$RunnerPy,
    [Parameter(Mandatory=$true)][string]$BasePy,
    [Parameter(Mandatory=$true)][int]$DeadPid,
    [Parameter(Mandatory=$true)][string]$OutFile
)
$ErrorActionPreference = "SilentlyContinue"
$script:CapturedLog = New-Object System.Collections.Generic.List[string]
function Write-Log($msg) { $script:CapturedLog.Add([string]$msg) }
$Root = $RootDir
$Py = $RunnerPy
$VenvPy = $RunnerPy
$FleetDir = Join-Path $Root ".fleet"
$FleetMarkerPath = Join-Path $Root ".fleet\fleet_run_active.json"
$env:MCP_FLEET_AUTORESUME = ""
'''

_DRIVER_FOOTER = r'''
function Write-DeadMarker {
    $m = @{ pid = $DeadPid; start_ts = 1.0; argv = @("--x"); resume_argv = @("--x") }
    Set-Content -Path $FleetMarkerPath -Value ($m | ConvertTo-Json) -Encoding ASCII
}
function Marker-Pid { $m = Get-FleetActiveMarker; if ($m) { return [int]$m.pid } else { return 0 } }
function Wait-For([scriptblock]$Cond, [int]$Seconds = 30) {
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) { if (& $Cond) { return $true }; Start-Sleep -Milliseconds 200 }
    return $false
}
$r = [ordered]@{}
$fake = $null
try {
    Write-DeadMarker

    # 1. a --resume coordinator of this checkout is already running (start_all's, or a person's)
    $fake = Start-Process -FilePath $BasePy -WindowStyle Hidden -PassThru -ArgumentList @(
        '-c', '"import time; time.sleep(120)"', 'relay.fleet_runner', '--resume', ('"' + $Root + '"'))
    Wait-For { @(Get-ThisCheckoutFleetCoordinatorPids) -contains $fake.Id } 15 | Out-Null
    $script:CapturedLog.Clear()
    $r.running = [ordered]@{
        fakePid = $fake.Id
        detected = @(Get-ThisCheckoutFleetCoordinatorPids)
        resumed = [bool](Invoke-FleetAutoResume -DryRun)
    }
    Invoke-FleetReap
    Invoke-FleetReap
    $r.running.markerPidAfterReaps = Marker-Pid
    $r.running.log = @($script:CapturedLog)
    Stop-Process -Id $fake.Id -Force
    $fake.WaitForExit(10000) | Out-Null

    # 2. control: nothing running -> the dry run would resume
    $script:CapturedLog.Clear()
    $r.control = [ordered]@{ resumed = [bool](Invoke-FleetAutoResume -DryRun); log = @($script:CapturedLog) }

    # 3. this supervisor resumes for real; the resumed run has not rewritten the marker yet
    $script:CapturedLog.Clear()
    $r.real = [ordered]@{ resumed = [bool](Invoke-FleetAutoResume) }
    $r.real.tracked = [bool]$script:ResumedFleet
    Invoke-FleetReap
    Invoke-FleetReap
    $r.real.markerPidWhileHeld = Marker-Pid
    $r.real.logWhileHeld = @($script:CapturedLog)

    # ...then it writes its own marker: the hold lifts, and the reaper leaves a live run alone
    New-Item -ItemType File -Path (Join-Path $FleetDir "GO") -Force | Out-Null
    $r.real.rewrote = Wait-For { (Marker-Pid) -ne $DeadPid -and (Marker-Pid) -ne 0 }
    $newPid = Marker-Pid
    $r.real.holdAfterRewrite = Get-FleetReapHoldReason
    Invoke-FleetReap
    $r.real.markerPidAfterLiveReap = Marker-Pid
    $r.real.newPid = $newPid

    # ...and once it has exited, the reap does its job again
    $launched = $script:AutoResumeRunners | Select-Object -First 1
    New-Item -ItemType File -Path (Join-Path $FleetDir "STOP") -Force | Out-Null
    $r.real.exited = Wait-For { try { $launched.Proc.Refresh(); $launched.Proc.HasExited } catch { $true } }
    $script:CapturedLog.Clear()
    Invoke-FleetReap
    $r.real.markerExistsAfterDeath = Test-Path $FleetMarkerPath
    $r.real.logAfterDeath = @($script:CapturedLog)

    # 4. a resumed run that dies BEFORE writing its own marker: the hold must lift by itself,
    # or a dead run's marker would never be reaped.
    Remove-Item (Join-Path $FleetDir "GO"), (Join-Path $FleetDir "STOP") -Force
    Write-DeadMarker
    $script:CapturedLog.Clear()
    $r.early = [ordered]@{ resumed = [bool](Invoke-FleetAutoResume) }
    Invoke-FleetReap
    $r.early.markerPidWhileHeld = Marker-Pid
    $early = $script:AutoResumeRunners | Select-Object -Last 1
    New-Item -ItemType File -Path (Join-Path $FleetDir "STOP") -Force | Out-Null
    $r.early.exited = Wait-For { try { $early.Proc.Refresh(); $early.Proc.HasExited } catch { $true } }
    Invoke-FleetReap
    $r.early.markerExistsAfterDeath = Test-Path $FleetMarkerPath
    $r.early.tracked = [bool]$script:ResumedFleet
    $r.early.log = @($script:CapturedLog)
} finally {
    if ($fake) { Stop-Process -Id $fake.Id -Force }
    New-Item -ItemType File -Path (Join-Path $FleetDir "GO") -Force | Out-Null
    New-Item -ItemType File -Path (Join-Path $FleetDir "STOP") -Force | Out-Null
    foreach ($e in $script:AutoResumeRunners) { try { $e.Proc.WaitForExit(10000) | Out-Null } catch { } }
    $r | ConvertTo-Json -Depth 8 | Set-Content -Path $OutFile -Encoding UTF8
}
'''

_FUNCTIONS = (
    "function Test-PythonRuns", "function Update-PythonInterpreter",
    "function Register-AutoResumeRunner", "function Test-FleetAutoResumeEnabled",
    "function Get-FleetActiveMarker", "function Test-PidAlive", "function Test-FleetShouldAutoResume",
    "function Get-ThisCheckoutFleetCoordinatorPids", "function Invoke-FleetAutoResume",
    "function Get-FleetReapHoldReason", "function Invoke-FleetReap",
)
_INIT_LINES = (
    "$script:PyRecheckSeconds =", "$script:PyRejectedAt = $null", "$script:PyRejectedWhy = $null",
    "$script:AutoResumeRunners = New-Object", "$script:ResumedFleet = $null",
    "$script:FleetReapHeldFor =",
)


def _venv_python() -> str:
    candidate = os.path.join(REPO, ".venv", "Scripts", "python.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


@pytest.fixture(scope="module")
def driver_result(src):
    work = tempfile.mkdtemp(prefix="sup_fleet_guard_")
    try:
        root = os.path.join(work, "repo")
        for d in (".fleet", "relay", "tools"):
            os.makedirs(os.path.join(root, d))
        shutil.copy(os.path.join(REPO, "relay", "fleet_reaper.py"), os.path.join(root, "relay"))
        for rel, body in (("relay/__init__.py", ""), ("relay/fleet_runner.py", _FAKE_RUNNER),
                          ("tools/__init__.py", ""), ("tools/notify_ops.py", _FAKE_NOTIFY)):
            with open(os.path.join(root, rel), "w", encoding="ascii") as fh:
                fh.write(body)
        dead = childproc.run([sys.executable, "-c", "import os; print(os.getpid())"], timeout=60,
                             creationflags=childproc.headless_creationflags())
        dead_pid = int(dead.stdout.strip().splitlines()[-1])

        parts = [_extract_line(src, l) for l in _INIT_LINES]
        parts += [_extract_braced_block(src, f) for f in _FUNCTIONS]
        driver = os.path.join(work, "driver.ps1")
        out = os.path.join(work, "out.json")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(_DRIVER_HEADER + "\n\n" + "\n\n".join(parts) + "\n\n" + _DRIVER_FOOTER)
        base_py = getattr(sys, "_base_executable", None) or sys.executable
        proc = childproc.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver,
             "-RootDir", root, "-RunnerPy", _venv_python(), "-BasePy", base_py,
             "-DeadPid", str(dead_pid), "-OutFile", out],
            timeout=300, creationflags=childproc.headless_creationflags())
        assert proc.returncode == 0, "driver failed:\n%s\n%s" % (proc.stdout, proc.stderr)
        with open(out, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        data["_deadPid"] = dead_pid
        data["_notified"] = os.path.exists(os.path.join(root, ".fleet", "NOTIFIED"))
        return data
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _lines(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


@windows_only
def test_no_second_coordinator_when_one_of_this_checkout_is_running(driver_result):
    r = driver_result["running"]
    assert r["fakePid"] in _lines(r["detected"]), "the running coordinator was not detected"
    assert r["resumed"] is False, "resumed while a coordinator of this checkout was running"
    lines = _lines(r["log"])
    assert any("already running (pid %d)" % r["fakePid"] in l for l in lines), lines


@windows_only
def test_a_running_resumer_also_withholds_the_reap_and_says_so_once(driver_result):
    r = driver_result["running"]
    assert r["markerPidAfterReaps"] == driver_result["_deadPid"], \
        "the reap deleted the marker while a --resume coordinator was running"
    held = [l for l in _lines(r["log"]) if "stale-run reap withheld" in l]
    assert len(held) == 1, "expected one 'withheld' line for two passes: %s" % _lines(r["log"])
    assert "--resume coordinator of this checkout" in held[0]


@windows_only
def test_with_nothing_running_the_resume_still_happens(driver_result):
    assert driver_result["control"]["resumed"] is True, _lines(driver_result["control"]["log"])


@windows_only
def test_the_first_reaps_after_a_real_resume_leave_its_marker_alone(driver_result):
    r = driver_result["real"]
    assert r["resumed"] is True and r["tracked"] is True
    assert r["markerPidWhileHeld"] == driver_result["_deadPid"], \
        "the reap deleted the marker of the run this supervisor had just resumed"
    assert any("resumed by this supervisor" in l for l in _lines(r["logWhileHeld"])), \
        _lines(r["logWhileHeld"])
    assert driver_result["_notified"] is True, "the resume path did not run to its end"


@windows_only
def test_the_hold_lifts_once_the_resumed_run_writes_its_marker_and_after_it_ends(driver_result):
    r = driver_result["real"]
    assert r["rewrote"] is True, "the stand-in runner never wrote its marker"
    assert not r["holdAfterRewrite"], "still withheld after the marker names a live pid"
    assert r["markerPidAfterLiveReap"] == r["newPid"], "the reaper touched a live run's marker"
    assert r["exited"] is True
    assert r["markerExistsAfterDeath"] is False, \
        "a dead run's marker was never reaped: the guard holds for ever"
    assert any("reaped stale fleet run" in l for l in _lines(r["logAfterDeath"])), \
        _lines(r["logAfterDeath"])


@windows_only
def test_a_resumed_run_that_dies_before_its_marker_releases_the_hold(driver_result):
    r = driver_result["early"]
    assert r["resumed"] is True
    assert r["markerPidWhileHeld"] == driver_result["_deadPid"], "reaped under a live resume"
    assert r["exited"] is True
    assert r["tracked"] is False, "the dead resumer is still held as if it were running"
    assert r["markerExistsAfterDeath"] is False, \
        "the resumer died before writing its marker and the reap stayed withheld: %s" % _lines(r["log"])
