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
def main():
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


# Only when launched as `-m relay.fleet_runner`: the supervisor also IMPORTS the real module
# (settings_disk_floor) to read the floor, and an import must not block.
if __name__ == "__main__":
    main()
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

    # 5. the per-cycle check reads the reaper's pending snapshot when the live marker is gone,
    # and in dry-run says so ONCE however many cycles look at it
    Remove-Item (Join-Path $FleetDir "GO"), (Join-Path $FleetDir "STOP") -Force -ErrorAction SilentlyContinue
    Remove-Item $FleetMarkerPath -Force -ErrorAction SilentlyContinue
    $snapDir = Join-Path $FleetDir "interrupted"
    New-Item -ItemType Directory -Path $snapDir -Force | Out-Null
    $snapBody = @{ schema = 1; run_id = "rTEST_a1"; state = "pending"; written_ts = 5.0
                   marker = @{ pid = $DeadPid; start_ts = 1.0; argv = @("--x"); resume_argv = @("--x") }
                   interrupted = @{ free_bytes_at_detection = 1000 }
                   resume = @{ count = 0; history = @() } }
    Set-Content -Path (Join-Path $snapDir "rTEST_a1.json") -Value ($snapBody | ConvertTo-Json -Depth 6) -Encoding ASCII
    $script:CapturedLog.Clear()
    $c1 = [bool](Invoke-FleetAutoResume -DryRun -FromCycle)
    $c2 = [bool](Invoke-FleetAutoResume -DryRun -FromCycle)
    $r.cycle = [ordered]@{ first = $c1; second = $c2; log = @($script:CapturedLog)
                           snapshotStillPending = ((Get-Content (Join-Path $snapDir "rTEST_a1.json") -Raw | ConvertFrom-Json).state) }
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
    "function Get-FleetResumeGate", "function Get-FleetPendingSnapshot",
    "function Write-FleetResumeLog",
    "function Get-FleetReapHoldReason", "function Invoke-FleetReap",
)
_INIT_LINES = (
    "$script:PyRecheckSeconds =", "$script:PyRejectedAt = $null", "$script:PyRejectedWhy = $null",
    "$script:AutoResumeRunners = New-Object", "$script:ResumedFleet = $null",
    "$script:FleetReapHeldFor =", "$script:FleetCycleNoted = @{}",
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


@windows_only
def test_the_cycle_check_resumes_from_a_pending_snapshot_in_dry_run_and_says_so_once(driver_result):
    r = driver_result["cycle"]
    assert r["first"] is True and r["second"] is True, _lines(r["log"])
    would = [l for l in _lines(r["log"]) if "auto-resuming" in l]
    dry = [l for l in _lines(r["log"]) if "DRY RUN" in l]
    assert len(would) == 1 and len(dry) == 1, "dry-run repeated every cycle: %s" % _lines(r["log"])
    assert r["snapshotStillPending"] == "pending", "a dry run changed the snapshot"


def test_the_cycle_call_is_governed_by_the_setting_not_by_a_command_line_flag(src):
    """The per-cycle resume used to be a DRY RUN unless -FleetCycleResumeLive was passed on the
    command line -- a switch the operator cannot reach from the cockpit, so for them the feature
    did not exist. It now runs live whenever the fleet_auto_resume setting is on (the default);
    the flag only forces it over a setting of off."""
    code = _code_only(src)
    call = "Invoke-FleetAutoResume -DryRun:$FleetResumeDryRun -FromCycle -ForceEnabled:$FleetCycleResumeLive"
    assert call in code
    assert "-DryRun:(-not $FleetCycleResumeLive)" not in code, "the cycle resume is dry-run by default again"
    assert code.index(call) > code.index("\n    Invoke-FleetReap\n"), \
        "the cycle resume must come after the reap"
    # and it comes BEFORE the queue drain, so a resumed run's launch guard exists when the router asks
    assert code.index(call) < code.index("\n    Invoke-QueueDrain\n"), \
        "the queue drain runs before the resume: queued goals would start a fresh coordinator first"


def test_the_switch_is_asked_only_with_a_candidate_and_reads_the_products_setting(src):
    body = _code_only(_extract_braced_block(src, "function Test-FleetAutoResumeEnabled"))
    assert "fleet_resume.auto_resume_setting()" in body, "the setting is not read through the product reader"
    assert "MCP_FLEET_AUTORESUME" in body and "$Force" in body, "the env override / -Force override is gone"
    inv = _code_only(_extract_braced_block(src, "function Invoke-FleetAutoResume"))
    assert inv.index("Test-FleetShouldAutoResume") < inv.index("Test-FleetAutoResumeEnabled"), \
        "every tick would spawn python to read the setting"


def test_the_launch_guard_is_written_before_the_launch_and_completed_after(src):
    inv = _code_only(_extract_braced_block(src, "function Invoke-FleetAutoResume"))
    first = inv.index("write_launch_guard(r'$FleetDir', 0,")
    launch = inv.index("Start-Process -FilePath $Py")
    done = inv.index("write_launch_guard(r'$FleetDir', $($fleetProc.Id)")
    assert first < launch < done
    assert inv.count("clear_launch_guard") == 2, "a failed launch must drop the guard"


def test_supervisor_ps1_additions_stay_ascii_in_the_resume_gate(src):
    gate = _extract_braced_block(src, "function Get-FleetResumeGate")
    assert gate.isascii()


# -- the PowerShell gate and relay.fleet_resume.resume_gate are one rule -----------------------

_NOW = 1_000_000.0
_GB = 1024 ** 3
_GATE_CASES = [
    # (name, record, free_bytes, floor_gb, signature, coordinator_live, enospc)
    ("fresh", {"state": "pending"}, 50 * _GB, None, "", False, False),
    ("stop", {"state": "pending", "stop_requested": True}, 50 * _GB, None, "", False, False),
    ("live", {"state": "pending"}, 50 * _GB, None, "", True, False),
    ("resumed", {"state": "resumed"}, 50 * _GB, None, "", False, False),
    ("gave_up", {"state": "gave_up"}, 50 * _GB, None, "", False, False),
    ("cap", {"state": "pending", "resume": {"count": 3, "last_ts": 1.0}}, 50 * _GB, None, "", False, False),
    ("backoff", {"state": "pending", "resume": {"count": 1, "last_ts": _NOW - 100}}, 50 * _GB, None, "", False, False),
    ("backoff_over", {"state": "pending", "resume": {"count": 1, "last_ts": _NOW - 601}}, 50 * _GB, None, "", False, False),
    ("backoff2", {"state": "pending", "resume": {"count": 2, "last_ts": _NOW - 1000}}, 50 * _GB, None, "", False, False),
    ("floor_below", {"state": "pending"}, 5 * _GB, 6.0, "", False, False),
    ("floor_ok", {"state": "pending"}, 7 * _GB, 6.0, "", False, False),
    ("floor_zero", {"state": "pending"}, 1 * _GB, 0, "", False, False),
    ("same_sig_no_gain", {"state": "pending", "resume": {"count": 1, "last_ts": 1.0, "last_signature": "abc", "last_free_bytes": 10 * _GB}},
     10 * _GB, None, "abc", False, False),
    ("same_sig_gain", {"state": "pending", "resume": {"count": 1, "last_ts": 1.0, "last_signature": "abc", "last_free_bytes": 10 * _GB}},
     11 * _GB, None, "abc", False, False),
    ("other_sig", {"state": "pending", "resume": {"count": 1, "last_ts": 1.0, "last_signature": "abc", "last_free_bytes": 10 * _GB}},
     10 * _GB, None, "def", False, False),
    ("enospc_no_gain", {"state": "pending", "interrupted": {"free_bytes_at_detection": 100}}, 100, None, "", False, True),
    ("enospc_gain", {"state": "pending", "interrupted": {"free_bytes_at_detection": 100}}, 10 * _GB, None, "", False, True),
    ("enospc_death_field", {"state": "pending", "interrupted": {"free_bytes_at_death": 100}}, 50, None, "", False, True),
]


def test_the_python_gate_gives_the_reasons_the_design_names():
    from relay.fleet_resume import resume_gate
    want = {"fresh": "ok", "stop": "stop_requested", "live": "coordinator_live",
            "resumed": "state_resumed", "gave_up": "state_gave_up", "cap": "max_resumes",
            "backoff": "backoff", "backoff_over": "ok", "backoff2": "backoff",
            "floor_below": "below_floor", "floor_ok": "ok", "floor_zero": "ok",
            "same_sig_no_gain": "same_crash_no_more_space", "same_sig_gain": "ok",
            "other_sig": "ok", "enospc_no_gain": "disk_full_no_more_space",
            "enospc_gain": "ok", "enospc_death_field": "disk_full_no_more_space"}
    for name, rec, free, floor, sig, live, enospc in _GATE_CASES:
        ok, reason = resume_gate(rec, _NOW, free, floor, sig, coordinator_live=live, enospc=enospc)
        assert reason == want[name], (name, reason)
        assert ok == (reason == "ok")


@windows_only
def test_the_powershell_gate_agrees_with_the_python_gate_on_every_case(src):
    from relay.fleet_resume import resume_gate
    work = tempfile.mkdtemp(prefix="sup_gate_parity_")
    try:
        cases = [{"name": n, "record": rec, "free": free, "floor": floor, "sig": sig,
                  "live": live, "enospc": enospc}
                 for n, rec, free, floor, sig, live, enospc in _GATE_CASES]
        cases_path = os.path.join(work, "cases.json")
        with open(cases_path, "w", encoding="ascii") as fh:
            json.dump(cases, fh)
        driver = os.path.join(work, "driver.ps1")
        out = os.path.join(work, "out.json")
        body = (
            "param([string]$CasesFile, [string]$OutFile, [double]$Now)\n"
            "$ErrorActionPreference = 'Stop'\n"
            + _extract_braced_block(src, "function Get-FleetResumeGate") + "\n"
            "$res = [ordered]@{}\n"
            "foreach ($c in (Get-Content $CasesFile -Raw | ConvertFrom-Json)) {\n"
            "  $res[$c.name] = Get-FleetResumeGate -Record $c.record -Now $Now -FreeBytes $c.free "
            "-FloorGb $c.floor -Signature ([string]$c.sig) -CoordinatorLive:([bool]$c.live) "
            "-Enospc:([bool]$c.enospc)\n"
            "}\n"
            "$res | ConvertTo-Json | Set-Content -Path $OutFile -Encoding ASCII\n")
        with open(driver, "w", encoding="ascii") as fh:
            fh.write(body)
        proc = childproc.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver,
             "-CasesFile", cases_path, "-OutFile", out, "-Now", str(_NOW)],
            timeout=120, creationflags=childproc.headless_creationflags())
        assert proc.returncode == 0, proc.stdout + proc.stderr
        with open(out, "r", encoding="utf-8-sig") as fh:
            ps = json.load(fh)
        for n, rec, free, floor, sig, live, enospc in _GATE_CASES:
            ok, reason = resume_gate(rec, _NOW, free, floor, sig, coordinator_live=live, enospc=enospc)
            assert ps[n] == reason, "case %s: powershell says %r, python says %r" % (n, ps[n], reason)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- the fleet_auto_resume setting drives the cycle resume, end to end through the real gate ------
#
# Same temp checkout and stand-in coordinator as above, but this one carries the REAL
# relay/fleet_resume.py and tools/settings_path.py, so the setting is read, the snapshot is
# updated, the decision is recorded and the launch guard is written by the product's own code.
# APPDATA points at an empty folder: settings_path falls back to %APPDATA% when the checkout has
# no .config/settings.txt, and the operator's real settings must not leak into a test.

_SETTING_DRIVER_FOOTER = r'''
$env:APPDATA = Join-Path $RootDir "appdata"
$SettingsPath = Join-Path $Root ".config\settings.txt"
New-Item -ItemType Directory -Path (Join-Path $Root ".config") -Force | Out-Null
$snapDir = Join-Path $FleetDir "interrupted"
New-Item -ItemType Directory -Path $snapDir -Force | Out-Null
$StopFile = Join-Path $FleetDir "STOP"

function Put-Setting($v) {
    if ($null -eq $v) { Remove-Item $SettingsPath -Force -ErrorAction SilentlyContinue }
    else { Set-Content -Path $SettingsPath -Value ("fleet_auto_resume=" + $v) -Encoding ASCII }
}
function New-Snap($id, $res, $extra) {
    Get-ChildItem $snapDir -Filter *.json | Remove-Item -Force
    $body = @{ schema = 1; run_id = $id; state = "pending"
               written_ts = [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
               marker = @{ pid = $DeadPid; start_ts = 1.0; argv = @("--x"); resume_argv = @("--x") }
               interrupted = @{ free_bytes_at_detection = 1000 }
               resume = $res }
    if ($extra) { foreach ($k in $extra.Keys) { $body[$k] = $extra[$k] } }
    Set-Content -Path (Join-Path $snapDir "$id.json") -Value ($body | ConvertTo-Json -Depth 6) -Encoding ASCII
}
function Read-Json($p) { if (Test-Path $p) { return (Get-Content $p -Raw | ConvertFrom-Json) } else { return $null } }
function Snap-State($id) { $d = Read-Json (Join-Path $snapDir "$id.json"); if ($d) { return [string]$d.state } else { return "" } }
function Decision() { $d = Read-Json (Join-Path $FleetDir "auto_resume_state.json"); if ($d) { return ($d.decision + ":" + $d.reason) } else { return "" } }
function Runner-Count() { return $script:AutoResumeRunners.Count }
function Stop-Fakes {
    New-Item -ItemType File -Path $StopFile -Force | Out-Null
    foreach ($e in $script:AutoResumeRunners.ToArray()) { try { $e.Proc.WaitForExit(20000) | Out-Null } catch { } }
    Remove-Item $StopFile -Force -ErrorAction SilentlyContinue
}
function Case($name, [scriptblock]$body) {
    $script:CapturedLog.Clear()
    Remove-Item (Join-Path $FleetDir "resume_launch.json"), (Join-Path $FleetDir "auto_resume_state.json") -Force -ErrorAction SilentlyContinue
    $before = Runner-Count
    $o = [ordered]@{}
    & $body $o
    $o.launched = (Runner-Count) - $before
    $o.log = @($script:CapturedLog)
    $o.decision = Decision
    $r[$name] = $o
    Stop-Fakes
}
$r = [ordered]@{}
try {
    # A. setting absent = the registry default (on): resumed ONCE, guard written with the pid
    Put-Setting $null
    Case "default_on" { param($o)
        New-Snap "rA_1" @{ count = 0; history = @() } $null
        $o.first = [bool](Invoke-FleetAutoResume -FromCycle)
        $o.afterFirst = Runner-Count
        $o.second = [bool](Invoke-FleetAutoResume -FromCycle)
        $o.afterSecond = Runner-Count
        $o.snapState = Snap-State "rA_1"
        $g = Read-Json (Join-Path $FleetDir "resume_launch.json")
        $o.guardPid = if ($g) { [int]$g.pid } else { -1 }
        $o.guardRun = if ($g) { [string]$g.run_id } else { "" }
        $o.runnerPid = $script:AutoResumeRunners[$script:AutoResumeRunners.Count - 1].Proc.Id
    }
    # B. setting off: never launched, the snapshot stays pending for a manual resume
    Put-Setting "off"
    Case "off" { param($o)
        New-Snap "rB_1" @{ count = 0; history = @() } $null
        $o.resumed = [bool](Invoke-FleetAutoResume -FromCycle)
        $o.again = [bool](Invoke-FleetAutoResume -FromCycle)
        $o.snapState = Snap-State "rB_1"
        $o.guardExists = Test-Path (Join-Path $FleetDir "resume_launch.json")
    }
    # C. off + the old -FleetCycleResumeLive switch (now -ForceEnabled): still an override
    Case "off_forced" { param($o)
        New-Snap "rC_1" @{ count = 0; history = @() } $null
        $o.resumed = [bool](Invoke-FleetAutoResume -FromCycle -ForceEnabled)
    }
    # D. MCP_FLEET_AUTORESUME beats the setting in BOTH directions
    Put-Setting "on"
    $env:MCP_FLEET_AUTORESUME = "0"
    Case "env_off_over_on" { param($o)
        New-Snap "rD_1" @{ count = 0; history = @() } $null
        $o.resumed = [bool](Invoke-FleetAutoResume -FromCycle)
        $o.snapState = Snap-State "rD_1"
    }
    Put-Setting "off"
    $env:MCP_FLEET_AUTORESUME = "1"
    Case "env_on_over_off" { param($o)
        New-Snap "rD_2" @{ count = 0; history = @() } $null
        $o.resumed = [bool](Invoke-FleetAutoResume -FromCycle)
    }
    $env:MCP_FLEET_AUTORESUME = ""
    # E. the loop guard still decides when the setting is on
    Put-Setting "on"
    Case "loop_cap" { param($o)
        New-Snap "rE_1" @{ count = 3; last_ts = 1.0; history = @() } $null
        $o.resumed = [bool](Invoke-FleetAutoResume -FromCycle)
        $o.snapState = Snap-State "rE_1"
    }
    Case "stop_requested" { param($o)
        New-Snap "rF_1" @{ count = 0; history = @() } @{ stop_requested = $true }
        $o.resumed = [bool](Invoke-FleetAutoResume -FromCycle)
        $o.snapState = Snap-State "rF_1"
    }
    Case "backoff" { param($o)
        New-Snap "rG_1" @{ count = 1; last_ts = [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); history = @() } $null
        $o.resumed = [bool](Invoke-FleetAutoResume -FromCycle)
        $o.snapState = Snap-State "rG_1"
    }
    Case "coordinator_already_live" { param($o)
        New-Snap "rH_1" @{ count = 0; history = @() } $null
        $fake = Start-Process -FilePath $BasePy -WindowStyle Hidden -PassThru -ArgumentList @(
            '-c', '"import time; time.sleep(120)"', 'relay.fleet_runner', '--resume', ('"' + $Root + '"'))
        $until = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $until -and -not (@(Get-ThisCheckoutFleetCoordinatorPids) -contains $fake.Id)) { Start-Sleep -Milliseconds 200 }
        $o.resumed = [bool](Invoke-FleetAutoResume -FromCycle)
        $o.snapState = Snap-State "rH_1"
        Stop-Process -Id $fake.Id -Force
    }
} finally {
    New-Item -ItemType File -Path (Join-Path $FleetDir "GO") -Force | Out-Null
    New-Item -ItemType File -Path $StopFile -Force | Out-Null
    foreach ($e in $script:AutoResumeRunners.ToArray()) { try { $e.Proc.WaitForExit(10000) | Out-Null } catch { } }
    $r | ConvertTo-Json -Depth 8 | Set-Content -Path $OutFile -Encoding UTF8
}
'''


@pytest.fixture(scope="module")
def setting_result(src):
    work = tempfile.mkdtemp(prefix="sup_fleet_setting_")
    try:
        root = os.path.join(work, "repo")
        for d in (".fleet", "relay", "tools"):
            os.makedirs(os.path.join(root, d))
        for rel in ("relay/fleet_reaper.py", "relay/fleet_resume.py", "tools/settings_path.py"):
            shutil.copy(os.path.join(REPO, *rel.split("/")), os.path.join(root, *rel.split("/")))
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
            fh.write(_DRIVER_HEADER + "\n\n" + "\n\n".join(parts) + "\n\n" + _SETTING_DRIVER_FOOTER)
        base_py = getattr(sys, "_base_executable", None) or sys.executable
        env = dict(os.environ)
        env.pop("MCP_FLEET_AUTORESUME", None)
        proc = childproc.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver,
             "-RootDir", root, "-RunnerPy", _venv_python(), "-BasePy", base_py,
             "-DeadPid", str(dead_pid), "-OutFile", out],
            timeout=420, creationflags=childproc.headless_creationflags(), env=env)
        assert proc.returncode == 0, "driver failed:\n%s\n%s" % (proc.stdout, proc.stderr)
        with open(out, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@windows_only
def test_setting_on_resumes_an_interrupted_run_exactly_once(setting_result):
    r = setting_result["default_on"]
    assert r["first"] is True and r["afterFirst"] >= 1, _lines(r["log"])
    assert r["second"] is False, "a second resume was launched for the same interrupted run"
    assert r["afterSecond"] == r["afterFirst"] and r["launched"] == 1, r
    assert r["snapState"] == "resumed"
    assert r["decision"] == "resumed:"


@windows_only
def test_a_resume_writes_a_launch_guard_naming_the_process_it_started(setting_result):
    """This is what stops the queue from starting a fresh coordinator in the same cycle."""
    r = setting_result["default_on"]
    assert r["guardPid"] == r["runnerPid"], "the guard does not name the launched coordinator: %s" % r
    assert r["guardRun"] == "rA_1"


@windows_only
def test_setting_off_never_launches_and_leaves_the_snapshot_pending(setting_result):
    r = setting_result["off"]
    assert r["resumed"] is False and r["again"] is False
    assert r["launched"] == 0
    assert r["snapState"] == "pending", "the snapshot was consumed with auto-resume off"
    assert r["guardExists"] is False
    assert any("OFF" in l for l in _lines(r["log"])), _lines(r["log"])


@windows_only
def test_the_old_cycle_switch_and_the_env_var_are_still_overrides(setting_result):
    assert setting_result["off_forced"]["resumed"] is True, "-ForceEnabled no longer overrides off"
    assert setting_result["env_off_over_on"]["resumed"] is False
    assert setting_result["env_off_over_on"]["launched"] == 0
    assert setting_result["env_off_over_on"]["snapState"] == "pending"
    assert setting_result["env_on_over_off"]["resumed"] is True


@windows_only
def test_the_loop_guard_still_refuses_with_the_setting_on(setting_result):
    cap = setting_result["loop_cap"]
    assert cap["resumed"] is False and cap["launched"] == 0
    assert cap["snapState"] == "gave_up", "the cap was not recorded"
    assert cap["decision"] == "refused:max_resumes"
    stop = setting_result["stop_requested"]
    assert stop["resumed"] is False and stop["launched"] == 0
    assert stop["decision"] == "refused:stop_requested"
    back = setting_result["backoff"]
    assert back["resumed"] is False and back["launched"] == 0
    assert back["snapState"] == "pending", "a backoff must leave the run to be resumed later"
    assert back["decision"] == "waiting:backoff"


@windows_only
def test_no_second_coordinator_even_with_the_setting_on(setting_result):
    r = setting_result["coordinator_already_live"]
    assert r["resumed"] is False and r["launched"] == 0
    assert r["snapState"] == "pending"
