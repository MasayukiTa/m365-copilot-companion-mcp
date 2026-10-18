# -*- coding: utf-8 -*-
r"""A supervisor that is KILLED (no catchable exit) must still leave its last-seen state behind.

ROOT CAUSE (see scripts/supervisor.ps1, the SUPERVISOR FORENSICS block and
Invoke-SupervisorPreviousDeathCheck's own header). scripts/supervisor.ps1 disappeared twice
without a trace: started 2026-10-18 03:56:06, gone before 15:11, and its log has NO 'leaving its
loop' record. PR 176 added a try/finally that writes that line on exit, a terminating error, or a
console close -- but a HARD KILL (Task Manager, an OOM reap, a session teardown) runs no finally
block BY DEFINITION, so the death leaves nothing to read. The child python main.py survived both
times, which is exactly the shape of the supervisor being singled out and killed while the machine
was saturated (free RAM was ~1 GB), but that is a hypothesis the log could not confirm.

THE FIX, in two halves, both checked here:

  * HEARTBEAT -- every loop iteration the supervisor overwrites a tiny .fleet file with its pid,
    iteration, local time and a cheap resource snapshot (free RAM, committed %, total CPU, its own
    working set). A hard kill cannot erase the LAST beat, so the last-seen moment survives even
    when the exit does not.
  * POST-MORTEM -- on the NEXT start, before the loop, the supervisor reads the previous heartbeat;
    if its pid is dead AND no 'leaving its loop' record follows it, it records a hard kill (one log
    line, supervisor_last_death.json, and a capped jsonl history). A CLEAN previous exit (the
    try/finally line is present) is left alone, so this never cries wolf over an ordinary restart.

WHY EXTRACT RATHER THAN DOT-SOURCE (same reasoning as
scripts/test_a_silent_death_leaves_its_exit_code.py): supervisor.ps1 dot-sources into a
`while ($true)` main loop behind an OS-wide single-instance mutex, so loading the whole file here
would hang or contend with the real supervisor on this machine. This test parses the .ps1 TEXT for
the forensics function blocks (balanced braces) and runs just those, plus a small driver, in an
isolated PowerShell process.

Windows-only: supervisor.ps1 is Windows-only (WMI/CIM, devtunnel.exe), and the runtime half reads
real Windows perf counters and launches real child processes with known, short lives.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402  (see: repository ratchet against text=True)

SUPERVISOR_PS1 = os.path.join(REPO, "scripts", "supervisor.ps1")

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason=(
        "scripts/supervisor.ps1 and this test are Windows/PowerShell-only "
        "(os.name=%r, powershell found=%r)" % (os.name, bool(_POWERSHELL))
    ),
)


# -- extracting the functions under test from the live .ps1, without dot-sourcing it ---------

def _extract_braced_block(text: str, start_marker: str) -> str:
    """The balanced-brace block starting at the first '{' at/after `start_marker`.

    NOT PowerShell-aware (see scripts/test_a_silent_death_leaves_its_exit_code.py's identical
    helper): every brace pair inside a string literal in the forensics functions -- the few
    `${prevPid}` / `${}` interpolations -- is itself balanced, so naive counting still lands on
    the right closing brace.
    """
    idx = text.index(start_marker)
    brace_start = text.index("{", idx)
    depth = 0
    for i in range(brace_start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[idx:i + 1]
    raise AssertionError("unbalanced braces extracting block at %r" % (start_marker,))


def _strip_ps_comment_lines(text: str) -> str:
    """Drop whole-line `#` comments so a source-level search matches CODE, not prose that names a
    pattern -- the same care scripts/test_a_silent_death_leaves_its_exit_code.py takes."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


@pytest.fixture(scope="module")
def supervisor_source() -> str:
    # utf-8-sig: the shipped file carries a BOM, and leaving it on the front of the first line
    # would break the str.index() anchors below.
    with open(SUPERVISOR_PS1, "r", encoding="utf-8-sig") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def forensics_functions(supervisor_source: str) -> str:
    names = (
        "function Get-SupervisorHeartbeatSample",
        "function Write-SupervisorHeartbeat",
        "function Test-SupervisorExitRecordedAfter",
        "function Invoke-SupervisorPreviousDeathCheck",
    )
    return "\n\n".join(_extract_braced_block(supervisor_source, n) for n in names)


def _venv_python() -> str:
    candidate = os.path.join(REPO, ".venv", "Scripts", "python.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


# -- source-level checks ---------------------------------------------------------------------

def test_the_heartbeat_is_written_every_loop_iteration(supervisor_source):
    """A heartbeat written once at startup would be stale the moment the loop ran. It must be
    inside the `while ($true)` body, after the iteration counter is bumped."""
    code = _strip_ps_comment_lines(supervisor_source)
    loop_idx = code.index("while ($true) {")
    body = code[loop_idx:]
    assert "$script:HeartbeatIteration++" in body, \
        "the loop never advances the heartbeat iteration counter"
    assert "Write-SupervisorHeartbeat -Iteration $script:HeartbeatIteration" in body, \
        "the loop body does not write a heartbeat each iteration"


def test_the_post_mortem_runs_at_startup_before_the_loop(supervisor_source):
    """The previous heartbeat must be read BEFORE this process's own loop overwrites it, or the
    evidence of the last death is gone before anyone looks."""
    code = _strip_ps_comment_lines(supervisor_source)
    call_idx = code.index("Invoke-SupervisorPreviousDeathCheck\n")
    loop_idx = code.index("while ($true) {")
    assert call_idx < loop_idx, \
        "the post-mortem check runs at or after the main loop, so the heartbeat it reads is this " \
        "process's own, not the dead one's"


def test_both_records_are_written_atomically(forensics_functions):
    """A record read mid-write is a corrupt record. Both the heartbeat and the death record use
    the temp-file + Move-Item dance the rest of this script uses for its .fleet sidecars."""
    code = _strip_ps_comment_lines(forensics_functions)
    for needle in ("$script:SupervisorHeartbeatPath", "$script:SupervisorLastDeathPath"):
        move_count = len(re.findall(
            r"Move-Item -LiteralPath .*%s" % re.escape(needle), code))
        assert move_count >= 1, "%s is not written via a Move-Item rename" % needle


def test_the_cpu_reading_is_a_formatted_counter_not_a_sampled_one(forensics_functions):
    """Get-Counter blocks for its sampling interval (~1 s); the heartbeat must stay cheap, so the
    CPU figure comes from the already-computed Win32_PerfFormattedData_* value instead."""
    code = _strip_ps_comment_lines(forensics_functions)
    assert "Win32_PerfFormattedData_PerfOS_Processor" in code
    assert "Get-Counter" not in code, \
        "the heartbeat uses Get-Counter, which blocks for a full sampling interval"


def test_the_forensics_block_is_ascii(forensics_functions):
    """supervisor.ps1 is read in several encodings across its tooling; the code this change adds
    stays ASCII so none of them can mangle it."""
    assert all(ord(c) < 128 for c in forensics_functions), \
        "a non-ASCII character slipped into the forensics functions"


# -- runtime checks: the extracted functions against real heartbeats and real dead/live pids --

_DRIVER_HEADER = '''param(
    [Parameter(Mandatory=$true)][string]$PyExe,
    [Parameter(Mandatory=$true)][string]$WorkDir,
    [Parameter(Mandatory=$true)][string]$OutFile
)

$ErrorActionPreference = "Stop"

# The module-level state the forensics functions reference, re-created here so the extracted
# bodies run exactly as they do in the real script, without dot-sourcing the whole supervisor.
$SupFleetDir = Join-Path $WorkDir ".fleet"
New-Item -ItemType Directory -Force -Path $SupFleetDir | Out-Null
$Log = Join-Path $WorkDir "supervisor.log"
$SupervisorStartedUnix = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$script:SupervisorHeartbeatPath = Join-Path $SupFleetDir "supervisor_heartbeat.json"
$script:SupervisorLastDeathPath = Join-Path $SupFleetDir "supervisor_last_death.json"
$script:SupervisorDeathHistoryPath = Join-Path $SupFleetDir "supervisor_death_history.jsonl"
$script:SupervisorDeathHistoryMax = 50
$script:HeartbeatIteration = 0

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Out-File -FilePath $Log -Append -Encoding utf8
}
'''

# Plain string (not f/.format): the extracted PowerShell is full of literal '{'/'}'.
_DRIVER_FOOTER = r'''
function Start-DeadChild {
    # A python that exits immediately, so by the time we read its pid it is a known-dead pid.
    $p = Start-Process -FilePath $PyExe -ArgumentList '-c "import sys; sys.exit(0)"' -WindowStyle Hidden -PassThru
    $p.WaitForExit()
    return $p.Id
}

function Start-LiveChild {
    $p = Start-Process -FilePath $PyExe -ArgumentList '-c "import time; time.sleep(30)"' -WindowStyle Hidden -PassThru
    return $p
}

function Write-FakeHeartbeat([int]$DeadPid, [string]$LocalTime) {
    $hb = [ordered]@{
        pid = $DeadPid; started = 1; iteration = 3; local_time = $LocalTime;
        free_ram_mb = 1024; committed_pct = 95; cpu_pct = 98; working_set_mb = 40
    }
    [IO.File]::WriteAllText($script:SupervisorHeartbeatPath, ($hb | ConvertTo-Json -Compress),
        (New-Object Text.UTF8Encoding($false)))
}

function Reset-State {
    Remove-Item -LiteralPath $script:SupervisorLastDeathPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Log -Force -ErrorAction SilentlyContinue
}

$results = [ordered]@{}

try {
    # (1) Write-SupervisorHeartbeat writes a real, parseable beat with the expected keys and this
    #     process's own live pid.
    Write-SupervisorHeartbeat -Iteration 7
    $beat = Get-Content -LiteralPath $script:SupervisorHeartbeatPath -Raw | ConvertFrom-Json
    $results.beat = [ordered]@{
        pid_is_mine = ([int]$beat.pid -eq $PID)
        iteration = $beat.iteration
        has_local_time = [bool]$beat.local_time
        has_cpu_key = ($beat.PSObject.Properties.Name -contains 'cpu_pct')
        has_free_ram_key = ($beat.PSObject.Properties.Name -contains 'free_ram_mb')
        has_working_set_key = ($beat.PSObject.Properties.Name -contains 'working_set_mb')
    }

    # (2) Test-SupervisorExitRecordedAfter: a clean-exit line AT/AFTER the beat is recognised;
    #     a line BEFORE it, an unrelated line, and an unparseable beat time are all 'not recorded'.
    $hbTime = "2026-10-18 04:00:00"
    $after = @("2026-10-18 04:00:05  supervisor pid 1 leaving its loop (exit, error or console close)")
    $before = @("2026-10-18 03:59:00  supervisor pid 1 leaving its loop (exit, error or console close)")
    $unrelated = @("2026-10-18 04:00:05  supervisor up (tunnel=x port=8000)")
    $results.exit_recorded = [ordered]@{
        after_is_true = (Test-SupervisorExitRecordedAfter -LogLines $after -HeartbeatLocalTime $hbTime)
        before_is_false = (Test-SupervisorExitRecordedAfter -LogLines $before -HeartbeatLocalTime $hbTime)
        unrelated_is_false = (Test-SupervisorExitRecordedAfter -LogLines $unrelated -HeartbeatLocalTime $hbTime)
        garbage_time_is_false = (Test-SupervisorExitRecordedAfter -LogLines $after -HeartbeatLocalTime "not-a-time")
    }

    # (3a) THE DEATH CASE: heartbeat names a dead pid, log has no 'leaving its loop' after it.
    #      Expect a death record, a history line, and a log line.
    Reset-State
    $deadPid = Start-DeadChild
    Write-FakeHeartbeat -DeadPid $deadPid -LocalTime (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    Invoke-SupervisorPreviousDeathCheck
    $deathWritten = Test-Path -LiteralPath $script:SupervisorLastDeathPath
    $deathObj = $null
    if ($deathWritten) { $deathObj = Get-Content -LiteralPath $script:SupervisorLastDeathPath -Raw | ConvertFrom-Json }
    $logTxt = if (Test-Path $Log) { Get-Content -LiteralPath $Log -Raw } else { "" }
    $histLines = if (Test-Path $script:SupervisorDeathHistoryPath) { @(Get-Content -LiteralPath $script:SupervisorDeathHistoryPath) } else { @() }
    $results.death = [ordered]@{
        record_written = $deathWritten
        verdict = $(if ($deathObj) { [string]$deathObj.verdict } else { $null })
        prev_pid_matches = $(if ($deathObj) { ([int]$deathObj.prev_pid -eq $deadPid) } else { $false })
        free_ram_carried = $(if ($deathObj) { ([int]$deathObj.free_ram_mb -eq 1024) } else { $false })
        log_mentions_killed = ($logTxt -match 'killed or crashed hard')
        log_mentions_pid = ($logTxt -match ([string]$deadPid))
        history_has_a_line = ($histLines.Count -ge 1)
    }

    # (3b) THE CLEAN-EXIT CASE: same dead pid, but a 'leaving its loop' record follows the beat.
    #      Nothing extra must be written.
    Reset-State
    $hbLocal = (Get-Date).AddSeconds(-5).ToString('yyyy-MM-dd HH:mm:ss')
    Write-FakeHeartbeat -DeadPid $deadPid -LocalTime $hbLocal
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  supervisor pid $deadPid leaving its loop (exit, error or console close)" |
        Out-File -FilePath $Log -Append -Encoding utf8
    Invoke-SupervisorPreviousDeathCheck
    $results.clean_exit = [ordered]@{
        no_death_record = (-not (Test-Path -LiteralPath $script:SupervisorLastDeathPath))
    }

    # (3c) THE LIVE CASE: heartbeat names a pid that is still running -> nothing to report.
    Reset-State
    $live = Start-LiveChild
    try {
        Write-FakeHeartbeat -DeadPid $live.Id -LocalTime (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        Invoke-SupervisorPreviousDeathCheck
        $results.live = [ordered]@{
            no_death_record = (-not (Test-Path -LiteralPath $script:SupervisorLastDeathPath))
        }
    } finally {
        try { Stop-Process -Id $live.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
}
finally {
    $results | ConvertTo-Json -Depth 6 | Set-Content -Path $OutFile -Encoding UTF8
}
'''


def _run_driver(forensics_functions: str, py_exe: str, work_dir: str) -> dict:
    driver_path = os.path.join(work_dir, "driver.ps1")
    out_path = os.path.join(work_dir, "out.json")
    state_dir = os.path.join(work_dir, "state")
    os.makedirs(state_dir, exist_ok=True)
    driver_text = _DRIVER_HEADER + "\n\n" + forensics_functions + "\n\n" + _DRIVER_FOOTER
    with open(driver_path, "w", encoding="utf-8") as fh:
        fh.write(driver_text)

    proc = childproc.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver_path,
         "-PyExe", py_exe, "-WorkDir", state_dir, "-OutFile", out_path],
        timeout=120,
        creationflags=childproc.headless_creationflags(),
    )
    assert proc.returncode == 0, (
        "driver powershell exited %s\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (proc.returncode, proc.stdout, proc.stderr)
    )
    assert os.path.isfile(out_path), "driver did not write its output file: %s" % out_path
    with open(out_path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def driver_result(forensics_functions, tmp_path_factory) -> dict:
    work_dir = str(tmp_path_factory.mktemp("supervisor_forensics_driver"))
    return _run_driver(forensics_functions, _venv_python(), work_dir)


def test_a_heartbeat_names_its_own_pid_and_carries_the_resource_keys(driver_result):
    beat = driver_result["beat"]
    assert beat["pid_is_mine"] is True
    assert beat["iteration"] == 7
    assert beat["has_local_time"] is True
    assert beat["has_cpu_key"] is True
    assert beat["has_free_ram_key"] is True
    assert beat["has_working_set_key"] is True


def test_an_exit_record_is_only_credited_when_it_follows_the_last_beat(driver_result):
    rec = driver_result["exit_recorded"]
    assert rec["after_is_true"] is True
    assert rec["before_is_false"] is False
    assert rec["unrelated_is_false"] is False
    assert rec["garbage_time_is_false"] is False


def test_a_dead_pid_with_no_exit_record_is_recorded_as_a_hard_kill(driver_result):
    d = driver_result["death"]
    assert d["record_written"] is True
    assert d["verdict"] == "killed_or_crashed_hard"
    assert d["prev_pid_matches"] is True
    assert d["free_ram_carried"] is True
    assert d["log_mentions_killed"] is True
    assert d["log_mentions_pid"] is True
    assert d["history_has_a_line"] is True


def test_a_clean_previous_exit_is_left_alone(driver_result):
    """The post-mortem must not cry wolf over an ordinary restart: when the try/finally 'leaving
    its loop' line is present after the last beat, no death is recorded."""
    assert driver_result["clean_exit"]["no_death_record"] is True


def test_a_still_running_previous_pid_is_not_a_death(driver_result):
    """A live pid in the heartbeat means the supervisor is still up (or the mutex already turned
    this instance away); there is nothing to post-mortem."""
    assert driver_result["live"]["no_death_record"] is True
