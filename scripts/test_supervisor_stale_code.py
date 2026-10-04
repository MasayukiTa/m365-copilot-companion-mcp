# -*- coding: utf-8 -*-
r"""The supervisor knows when it is running older code than the checkout, and can replace itself.

THE INCIDENT (2026-10-04, found live). A PowerShell script is parsed once, when its process starts.
The long-running supervisor (started 20:04) kept running its pre-#122 text after the auto-resume
change was merged and the working tree updated, so when the coordinator died it logged "fleet
auto-resume DRY RUN -- not relaunching" and nothing resumed. The server, bridge and coordinator pick
up code on their own restarts and the cockpit is rebuilt by rebuild_ui.ps1; the supervisor had no
staleness handling at all.

WHAT IS PINNED HERE
  * fingerprint at start vs the disk: unchanged => not stale; a changed file => stale, listed, and
    published to .fleet/supervisor_state.json (what the cockpit's Recovery section reads);
  * the self-restart verdict: off, parse error, loop guard, coordinator running, interrupted
    snapshot pending, resume in progress, review/local-loop run, bridge busy or unknown each REFUSE;
    only an idle machine with a parsing script and an old-enough previous restart says "ok";
  * "ok" hands over (the old supervisor exits) and leaves the loop-guard mark; a handoff helper that
    cannot be started leaves the old supervisor running;
  * the handoff helper waits for the old process, starts the new supervisor, retries when it dies
    on startup, and never starts a second one beside a supervisor that is still alive;
  * the PowerShell file list and fingerprint equal the Python module's.

HOW THIS RUNS. Real PowerShell drivers over extracted functions (never dot-source supervisor.ps1:
forever loop, machine-wide mutex, a live supervisor on this machine) with a temp folder as $Root
that holds copies of the real scripts. Nothing touches this checkout's .fleet and no real supervisor
is started: Start-SupervisorHandoff is stubbed in the cycle tests, and the helper's own tests run it
against a fake supervisor.ps1 that only writes a file.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402
from relay import code_staleness  # noqa: E402

SUPERVISOR_PS1 = os.path.join(REPO, "scripts", "supervisor.ps1")
HANDOFF_PS1 = os.path.join(REPO, "scripts", "supervisor_handoff.ps1")
TUNNEL_UTIL_PS1 = os.path.join(REPO, "scripts", "tunnel_name_util.ps1")

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

windows_only = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason="supervisor.ps1 is Windows/PowerShell-only (os.name=%r, powershell found=%r)"
           % (os.name, bool(_POWERSHELL)),
)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _extract_braced_block(text, start_marker):
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


def _code_only(text):
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


@pytest.fixture(scope="module")
def src():
    return _read(SUPERVISOR_PS1)


_FUNCTIONS = (
    "function Get-SupervisorFileStat", "function Get-SupervisorFileHash", "function Get-SupervisorGitHead",
    "function Initialize-SupervisorCodeWatch", "function Update-SupervisorCodeState",
    "function Write-SupervisorState", "function Test-SupervisorScriptsParse",
    "function Get-SupervisorRestartVerdict", "function Get-SupervisorLastRestartAge",
    "function Invoke-SupervisorCodeCycle",
)

_DRIVER_HEAD = r'''param([string]$RootDir, [string]$OutDir)
$ErrorActionPreference = "Continue"
$Root = $RootDir
$Py = "python"
$BridgePort = 1
$Log = Join-Path $OutDir "log.txt"
$SupervisorStartedUnix = 12345
$FleetResumeDryRun = $false
$FleetCycleResumeLive = $false
$TunnelName = "t"; $Port = 8000; $IntervalSeconds = 15; $FailuresBeforeAction = 4; $StartupGraceSeconds = 180
$LocalLoopMarkerDir = Join-Path $Root ".fleet\local_loop_active"
function Write-Log($msg) { Add-Content -Path $Log -Value ([string]$msg) -Encoding UTF8 }
'''

_DRIVER_STUBS = r'''
function Get-SupervisorSelfRestartSetting { if ($env:T_SETTING) { return $env:T_SETTING } else { return "on" } }
function Get-ThisCheckoutFleetCoordinatorPids { if ($env:T_COORD) { return @(1234) } else { return @() } }
function Get-FleetPendingSnapshot { if ($env:T_SNAP) { return @{ Path = "x" } } else { return $null } }
function Test-SupervisorResumeInProgress { return [bool]$env:T_RESUME }
function Test-SupervisorOtherRunActive { return [bool]$env:T_RUN }
function Get-SupervisorBridgeState { if ($env:T_BRIDGE) { return $env:T_BRIDGE } else { return "idle" } }
function Start-SupervisorHandoff {
    Add-Content -Path (Join-Path $OutDir "handoff.txt") -Value "called" -Encoding ASCII
    if ($env:T_HANDOFF_FAILS) { return $false }
    return $true
}
'''

_DRIVER_RUN = r'''
Initialize-SupervisorCodeWatch
Write-SupervisorState
$sup = Join-Path $Root "scripts\supervisor.ps1"
if ($env:T_MUTATE -eq "comment") { Add-Content -Path $sup -Value "`n# a later edit" -Encoding ASCII }
if ($env:T_MUTATE -eq "broken") { Add-Content -Path $sup -Value "`nfunction Broken { (( " -Encoding ASCII }
if ($env:T_MARK_AGE) {
    $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [int]$env:T_MARK_AGE
    Set-Content -Path (Join-Path $Root ".fleet\supervisor_selfrestart.json") -Value ('{"ts":' + $ts + ',"from_pid":1,"files":[]}') -Encoding ASCII
}
Set-Content -Path (Join-Path $OutDir "pid.txt") -Value $PID -Encoding ASCII
Invoke-SupervisorCodeCycle
Invoke-SupervisorCodeCycle
Set-Content -Path (Join-Path $OutDir "returned.txt") -Value "yes" -Encoding ASCII
'''


def _make_root(work):
    root = os.path.join(work, "repo")
    os.makedirs(os.path.join(root, "scripts"))
    os.makedirs(os.path.join(root, ".fleet"))
    shutil.copy(SUPERVISOR_PS1, os.path.join(root, "scripts"))
    shutil.copy(TUNNEL_UTIL_PS1, os.path.join(root, "scripts"))
    return root


def _build_driver(work, src, body):
    parts = [_extract_braced_block(src, f) for f in _FUNCTIONS]
    decl_start = src.index("$SupFleetDir = ")
    decl_end = src.index("function Get-SupervisorFileStat")
    decls = src[decl_start:decl_end]
    # the helper-script path the parse check uses is $PSScriptRoot: the driver's own folder
    shutil.copy(HANDOFF_PS1, os.path.join(work, "supervisor_handoff.ps1"))
    driver = os.path.join(work, "driver.ps1")
    with open(driver, "w", encoding="utf-8") as fh:
        fh.write(_DRIVER_HEAD + "\n" + decls + "\n" + "\n\n".join(parts) + "\n" + _DRIVER_STUBS + "\n" + body)
    return driver


def _run_cycle(src, env=None):
    work = tempfile.mkdtemp(prefix="sup_stale_")
    try:
        root = _make_root(work)
        driver = _build_driver(work, src, _DRIVER_RUN)
        e = dict(os.environ)
        for k in list(e):
            if k.startswith("T_"):
                del e[k]
        e.update(env or {})
        proc = childproc.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver,
             "-RootDir", root, "-OutDir", work],
            timeout=180, env=e, creationflags=childproc.headless_creationflags())
        out = {"rc": proc.returncode, "stderr": proc.stderr, "stdout": proc.stdout}

        def rd(p):
            fp = os.path.join(work, p) if not os.path.isabs(p) else p
            return _read(fp) if os.path.exists(fp) else None
        out["returned"] = rd("returned.txt") is not None
        out["handoff_called"] = rd("handoff.txt") is not None
        out["log"] = rd("log.txt") or ""
        out["pid"] = int((rd("pid.txt") or "0").strip() or 0)
        st = rd(os.path.join(root, ".fleet", "supervisor_state.json"))
        out["state"] = json.loads(st.lstrip("\ufeff")) if st else None
        out["mark_exists"] = os.path.exists(os.path.join(root, ".fleet", "supervisor_selfrestart.json"))
        out["root"] = root
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- every platform: the lists and the wiring ------------------------------------------------------

def test_the_powershell_file_list_is_the_python_modules(src):
    line = [l for l in src.splitlines() if l.startswith("$script:SupCodeFiles = ")][0]
    ps = tuple(p for p in line.split("@(", 1)[1].rstrip(")").replace('"', "").replace(" ", "").split(",") if p)
    assert ps == code_staleness.SUPERVISOR_CODE_FILES
    for rel in ps:
        assert os.path.isfile(os.path.join(REPO, rel)), rel


def test_the_supervisor_dot_sources_exactly_what_is_fingerprinted(src):
    dotted = [l.strip() for l in src.splitlines() if l.lstrip().startswith(". (Join-Path $PSScriptRoot")]
    assert dotted, "the supervisor no longer dot-sources anything; revisit SUPERVISOR_CODE_FILES"
    for l in dotted:
        name = l.split('"')[1]
        assert "scripts/" + name in code_staleness.SUPERVISOR_CODE_FILES, \
            "%s is loaded by the supervisor but is not fingerprinted" % name


def test_the_check_runs_every_tick_and_the_record_is_taken_before_anything_slow(src):
    code = _code_only(src)
    loop = code.index("\nwhile ($true) {")
    assert code.index("Initialize-SupervisorCodeWatch\n") < code.index("Get-ServerExitRecord") < loop, \
        "the fingerprint must be recorded at the top of the script, before the long start-up work"
    tail = code[loop:]
    assert tail.index("Invoke-SupervisorCodeCycle") < tail.index("Wait-ForNextTick -Seconds"), \
        "the staleness check is not part of the tick"


def test_the_handoff_script_is_ascii_and_parses():
    data = open(HANDOFF_PS1, "rb").read()
    assert all(b < 128 for b in data), "supervisor_handoff.ps1 must be ASCII (cp932 console)"
    if os.name == "nt" and _POWERSHELL:
        proc = childproc.run(
            [_POWERSHELL, "-NoProfile", "-Command",
             "$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$t,[ref]$e);$e.Count" % HANDOFF_PS1],
            timeout=60, creationflags=childproc.headless_creationflags())
        assert proc.stdout.strip().splitlines()[-1] == "0", proc.stdout


# -- Windows: real PowerShell ----------------------------------------------------------------------

@windows_only
def test_unchanged_code_is_not_stale_and_nothing_happens(src):
    r = _run_cycle(src)
    assert r["rc"] == 0, r
    assert r["returned"] and not r["handoff_called"]
    assert r["state"]["supervisor"]["stale"] is False
    assert r["state"]["supervisor"]["changed_files"] == []
    assert r["state"]["pid"] == r["pid"] and r["state"]["start_ts"] == 12345
    assert "OLDER CODE" not in r["log"]


@windows_only
def test_the_recorded_fingerprint_is_the_python_one(src):
    r = _run_cycle(src)
    want = code_staleness.fingerprint(REPO, code_staleness.SUPERVISOR_CODE_FILES)
    assert r["state"]["fingerprint"] == want
    assert isinstance(r["state"]["git_head"], str)   # "" in the temp root (no .git): informational only


@windows_only
def test_a_changed_file_is_stale_logged_once_and_exported(src):
    r = _run_cycle(src, {"T_MUTATE": "comment", "T_SETTING": "off"})
    assert r["rc"] == 0, r
    sup = r["state"]["supervisor"]
    assert sup["stale"] is True and sup["changed_files"] == ["scripts/supervisor.ps1"]
    assert sup["pid"] == r["pid"]
    assert r["state"]["self_restart"]["verdict"] == "setting_off"
    assert r["log"].count("OLDER CODE") == 1, "two cycles must log the finding once:\n" + r["log"]
    assert "restart is needed" in r["log"]
    assert r["returned"] and not r["handoff_called"] and not r["mark_exists"]


@windows_only
def test_idle_machine_on_hands_over_and_exits_leaving_the_loop_guard_mark(src):
    r = _run_cycle(src, {"T_MUTATE": "comment"})
    assert r["handoff_called"]
    assert not r["returned"], "the old supervisor must exit after a confirmed handoff"
    assert r["rc"] == 0
    assert r["mark_exists"], "the loop guard mark must be written BEFORE the handoff"
    assert r["state"]["self_restart"]["verdict"] == "ok"
    assert "handing over to a fresh supervisor" in r["log"]


@windows_only
@pytest.mark.parametrize("env,verdict", [
    ({"T_COORD": "1"}, "coordinator_running"),
    ({"T_SNAP": "1"}, "snapshot_pending"),
    ({"T_RESUME": "1"}, "resume_in_progress"),
    ({"T_RUN": "1"}, "run_active"),
    ({"T_BRIDGE": "busy"}, "bridge_busy"),
    ({"T_BRIDGE": "unknown"}, "bridge_busy"),
    ({"T_MUTATE": "broken"}, "parse_error"),
    ({"T_MARK_AGE": "60"}, "loop_guard"),
])
def test_self_restart_refuses_unless_it_is_safe(src, env, verdict):
    e = {"T_MUTATE": "comment"}
    e.update(env)
    r = _run_cycle(src, e)
    assert r["rc"] == 0, r
    assert r["returned"] and not r["handoff_called"], r["log"]
    assert r["state"]["supervisor"]["stale"] is True
    assert r["state"]["self_restart"]["verdict"] == verdict
    if verdict == "parse_error":
        assert "does not parse" in r["log"] and "supervisor.ps1" in r["log"]
    if verdict == "loop_guard":
        assert r["mark_exists"]    # the previous restart's mark is untouched


@windows_only
def test_an_old_enough_previous_restart_does_not_hold_it_back(src):
    r = _run_cycle(src, {"T_MUTATE": "comment", "T_MARK_AGE": "700"})
    assert r["handoff_called"] and not r["returned"]


@windows_only
def test_a_handoff_that_cannot_start_leaves_the_old_supervisor_running(src):
    r = _run_cycle(src, {"T_MUTATE": "comment", "T_HANDOFF_FAILS": "1"})
    assert r["handoff_called"]
    assert r["returned"], "no supervisor may be left behind: the old one must stay up"
    assert "staying up on the old code" in r["log"]


@windows_only
def test_the_verdict_table(src):
    work = tempfile.mkdtemp(prefix="sup_verdict_")
    try:
        fn = _extract_braced_block(src, "function Get-SupervisorRestartVerdict")
        driver = os.path.join(work, "v.ps1")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(fn + r'''
function V { param($h) $p = @{ Stale = $true; Setting = "on"; ParseOk = $true }; foreach ($k in $h.Keys) { $p[$k] = $h[$k] }; Get-SupervisorRestartVerdict @p }
$o = [ordered]@{}
$o.not_stale = (Get-SupervisorRestartVerdict -Stale $false -Setting "on" -ParseOk $true)
$o.ok = (V @{})
$o.off = (V @{ Setting = "off" })
$o.parse = (V @{ ParseOk = $false })
$o.guard = (V @{ LastRestartAgeSeconds = 599.0 })
$o.guard_edge = (V @{ LastRestartAgeSeconds = 600.0 })
$o.never_restarted = (V @{ LastRestartAgeSeconds = -1.0 })
$o.coord_beats_snapshot = (V @{ CoordinatorCount = 1; SnapshotPending = $true })
$o.off_beats_everything = (Get-SupervisorRestartVerdict -Stale $true -Setting "off" -ParseOk $false -CoordinatorCount 2)
$o | ConvertTo-Json -Compress
''')
        proc = childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver],
                             timeout=120, creationflags=childproc.headless_creationflags())
        got = json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    assert got["not_stale"] == "not_stale"
    assert got["ok"] == "ok"
    assert got["off"] == "setting_off" and got["parse"] == "parse_error"
    assert got["guard"] == "loop_guard"
    assert got["guard_edge"] == "ok" and got["never_restarted"] == "ok"
    assert got["coord_beats_snapshot"] == "coordinator_running"
    assert got["off_beats_everything"] == "setting_off"


# -- the handoff helper, against a fake supervisor --------------------------------------------------

_FAKE_SUPERVISOR_OK = r'''param([string]$TunnelName = "", [int]$Port = 0, [int]$IntervalSeconds = 0,
      [int]$FailuresBeforeAction = 0, [int]$StartupGraceSeconds = 0, [switch]$FleetResumeDryRun,
      [switch]$FleetCycleResumeLive)
$out = Join-Path $PSScriptRoot "started.txt"
Add-Content -Path $out -Value ("tunnel=$TunnelName port=$Port interval=$IntervalSeconds dry=$([bool]$FleetResumeDryRun)") -Encoding ASCII
Start-Sleep -Seconds 12
'''

_FAKE_SUPERVISOR_DIES = r'''param([string]$TunnelName = "", [int]$Port = 0, [int]$IntervalSeconds = 0,
      [int]$FailuresBeforeAction = 0, [int]$StartupGraceSeconds = 0, [switch]$FleetResumeDryRun,
      [switch]$FleetCycleResumeLive)
Add-Content -Path (Join-Path $PSScriptRoot "started.txt") -Value "died" -Encoding ASCII
exit 3
'''


def _run_handoff(fake_body, old_pid, extra):
    work = tempfile.mkdtemp(prefix="sup_handoff_")
    try:
        shutil.copy(HANDOFF_PS1, os.path.join(work, "supervisor_handoff.ps1"))
        with open(os.path.join(work, "supervisor.ps1"), "w", encoding="ascii") as fh:
            fh.write(fake_body)
        log = os.path.join(work, "log.txt")
        proc = childproc.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             os.path.join(work, "supervisor_handoff.ps1"), "-OldPid", str(old_pid),
             "-TunnelName", "my-tunnel", "-Port", "8123", "-LogPath", log] + extra,
            timeout=180, creationflags=childproc.headless_creationflags())
        started = os.path.join(work, "started.txt")
        res = {"rc": proc.returncode, "log": _read(log) if os.path.exists(log) else "",
               "started": _read(started).splitlines() if os.path.exists(started) else []}
        if res["started"]:
            time.sleep(0.2)
        return res
    finally:
        # the fake sleeps 12 s at most; wait it out so the temp folder can go
        for _ in range(40):
            try:
                shutil.rmtree(work)
                break
            except OSError:
                time.sleep(0.5)


def _dead_pid():
    p = childproc.run([sys.executable, "-c", "import os; print(os.getpid())"], timeout=60,
                      creationflags=childproc.headless_creationflags())
    return int(p.stdout.strip().splitlines()[-1])


@windows_only
def test_the_helper_starts_the_new_supervisor_with_the_same_arguments_once_the_old_is_gone():
    r = _run_handoff(_FAKE_SUPERVISOR_OK, _dead_pid(), ["-SurviveSeconds", "2"])
    assert r["rc"] == 0, r
    assert r["started"] == ["tunnel=my-tunnel port=8123 interval=15 dry=False"], r
    assert "handoff complete" in r["log"]


@windows_only
def test_the_helper_retries_a_supervisor_that_dies_on_startup_and_says_when_none_is_running():
    r = _run_handoff(_FAKE_SUPERVISOR_DIES, _dead_pid(),
                     ["-SurviveSeconds", "3", "-Attempts", "2", "-RetryGapSeconds", "1"])
    assert r["rc"] == 3, r
    assert r["started"] == ["died", "died"], "two attempts expected: %r" % (r,)
    assert "NO SUPERVISOR IS RUNNING" in r["log"]


@windows_only
def test_the_helper_never_starts_a_second_supervisor_beside_a_live_one():
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                            creationflags=childproc.headless_creationflags())
    try:
        r = _run_handoff(_FAKE_SUPERVISOR_OK, live.pid, ["-WaitSeconds", "2"])
    finally:
        live.kill()
        live.wait(timeout=10)
    assert r["rc"] == 1, r
    assert r["started"] == [], "a second supervisor must not be started"
    assert "stays the supervisor" in r["log"]
