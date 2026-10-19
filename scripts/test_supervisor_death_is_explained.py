# -*- coding: utf-8 -*-
r"""A supervisor stop that start_all does ON PURPOSE must not read as "killed or crashed hard",
and a supervisor that dies for real must leave its exit code behind.

WHAT WAS MEASURED (2026-10-20, from the supervisor log, .setup/logs/start_all_runs.jsonl,
.setup/logs/deps_install_20261020_002658.log and the Windows PowerShell event log):

  * supervisor pid 23744 logged 'server check failed (4/4)' at 00:26:30 and 'MCP server process
    launched' at 00:26:37, then was gone. Its last heartbeat was 00:26:20.
  * At the same time start_all.ps1 (pid 29872, started 00:26:12 by FleetCockpit's repair.ps1
    -Auto) was in its deps_sync phase (123 s). The pip install log is stamped 00:26:58.
    Invoke-DependencySync stops "this checkout's supervisor, MCP server and bridge" with
    Stop-Process -Force when nothing is live, so the supervisor was ended by design.
  * Stop-Process -Force runs no `finally`, so the supervisor wrote no 'leaving its loop' record,
    and the next supervisor (00:28:50) logged 'no exit record -> it was killed or crashed hard'.
    That line was WRONG for this death: a planned stop was reported as a crash.

THE FIX, checked here:
  * start_all.ps1 writes the 'leaving its loop' record on the supervisor's behalf, naming itself
    and the reason, BEFORE it stops the supervisor (both call sites: the dependency sync and the
    stale-tunnel restart). The supervisor's own post-mortem then finds the record and stays quiet.
  * scripts/supervisor_exit_watch.ps1, started by the supervisor for itself, waits on the
    supervisor's process and writes its exit code to the supervisor log. A kill that nobody
    recorded (the 2026-10-19 15:30 death has no cause in any log) leaves an exit code and the
    count of surviving powershell.exe processes, which tells a single targeted kill (code 1, many
    shells alive) from a crash (0xC0000005) from a logoff (0xC000013A) from a mass kill.

Windows-only; the runtime parts start real, short-lived child processes of their own and never
touch the real supervisor, server, tunnel or ports.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

START_ALL = os.path.join(REPO, "scripts", "start_all.ps1")
SUPERVISOR = os.path.join(REPO, "scripts", "supervisor.ps1")
WATCHER = os.path.join(REPO, "scripts", "supervisor_exit_watch.ps1")

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason="supervisor.ps1 / start_all.ps1 are Windows/PowerShell-only",
)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()


def _strip_comments(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


def _extract_braced_block(text: str, start_marker: str) -> str:
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
    raise AssertionError("unbalanced braces at %r" % (start_marker,))


def _run_ps(script_path: str, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path, *args],
        capture_output=True, timeout=timeout,
    )


# -- the planned stop is recorded ------------------------------------------------------------

def test_start_all_defines_the_planned_stop_record():
    code = _strip_comments(_read(START_ALL))
    assert "function Write-SupervisorPlannedStopRecord" in code


def test_every_supervisor_stop_in_start_all_records_first():
    """Each loop that Stop-Process'es supervisor processes must call the record writer on the same
    process BEFORE the Stop-Process, in the same loop body."""
    code = _strip_comments(_read(START_ALL))
    sites = [
        m for m in re.finditer(
            r"foreach \(\$p in (\$sups|\$runningSupervisors)\) \{(?P<body>[^\n]*(?:\n[^\n]*){0,3})",
            code)
    ]
    assert len(sites) == 2, "expected the deps-sync and the stale-tunnel stop sites, got %d" % len(sites)
    for m in sites:
        body = m.group("body")
        assert "Stop-Process" in body, "site %s does not stop the process in its first lines" % m.group(1)
        rec = body.find("Write-SupervisorPlannedStopRecord")
        stop = body.find("Stop-Process")
        assert 0 <= rec < stop, "site %s stops the supervisor before (or without) recording it" % m.group(1)


_REC_DRIVER = r'''param([string]$FuncFile, [string]$LogPath, [int]$TargetPid, [string]$Reason)
$ErrorActionPreference = "Stop"
. $FuncFile
Write-SupervisorPlannedStopRecord -ProcessId $TargetPid -Reason $Reason -LogPath $LogPath
'''

_CHECK_DRIVER = r'''param([string]$FuncFile, [string]$LogPath, [string]$Heartbeat, [string]$OutFile)
$ErrorActionPreference = "Stop"
. $FuncFile
$lines = @()
if (Test-Path -LiteralPath $LogPath) { $lines = @(Get-Content -LiteralPath $LogPath) }
$r = Test-SupervisorExitRecordedAfter -LogLines $lines -HeartbeatLocalTime $Heartbeat
Set-Content -LiteralPath $OutFile -Value ([string]$r) -Encoding ASCII
'''


def test_the_record_is_one_the_post_mortem_accepts(tmp_path):
    """Round trip through the REAL post-mortem predicate (extracted from supervisor.ps1): a log
    with the start_all record after the last heartbeat counts as a recorded exit; the same log
    without it does not."""
    start_all = _read(START_ALL)
    sup = _read(SUPERVISOR)
    rec_fn = _extract_braced_block(start_all, "function Write-SupervisorPlannedStopRecord")
    chk_fn = _extract_braced_block(sup, "function Test-SupervisorExitRecordedAfter")

    funcs = tmp_path / "funcs.ps1"
    funcs.write_text(rec_fn + "\n\n" + chk_fn + "\n", encoding="ascii")
    rec_driver = tmp_path / "rec.ps1"
    rec_driver.write_text(_REC_DRIVER, encoding="ascii")
    chk_driver = tmp_path / "chk.ps1"
    chk_driver.write_text(_CHECK_DRIVER, encoding="ascii")
    log = tmp_path / "sup.log"
    out = tmp_path / "out.txt"
    heartbeat = "2000-01-01 00:00:00"   # long before 'now': any record written now is after it

    def verdict() -> str:
        r = _run_ps(str(chk_driver), "-FuncFile", str(funcs), "-LogPath", str(log),
                    "-Heartbeat", heartbeat, "-OutFile", str(out))
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
        return out.read_text(encoding="ascii").strip()

    log.write_text("2000-01-01 00:00:00  supervisor up\n", encoding="utf-8")
    assert verdict() == "False"            # nothing recorded -> would be reported as a hard kill

    r = _run_ps(str(rec_driver), "-FuncFile", str(funcs), "-LogPath", str(log),
                "-TargetPid", "4242", "-Reason", "dependency sync")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    text = log.read_text(encoding="utf-8-sig")
    assert "supervisor pid 4242 leaving its loop" in text
    assert "start_all" in text and "dependency sync" in text
    assert verdict() == "True"             # recorded -> the post-mortem stays quiet


def test_the_record_writer_never_throws_on_an_unwritable_log(tmp_path):
    start_all = _read(START_ALL)
    rec_fn = _extract_braced_block(start_all, "function Write-SupervisorPlannedStopRecord")
    funcs = tmp_path / "funcs.ps1"
    funcs.write_text(rec_fn + "\n", encoding="ascii")
    drv = tmp_path / "rec.ps1"
    drv.write_text(_REC_DRIVER, encoding="ascii")
    bad = tmp_path / "no_such_dir" / "deeper" / "x.log"
    r = _run_ps(str(drv), "-FuncFile", str(funcs), "-LogPath", str(bad),
                "-TargetPid", "1", "-Reason", "x")
    assert r.returncode == 0, "a log that cannot be written must not stop start_all"


# -- the unrecorded death leaves an exit code -----------------------------------------------

def _spawn_victim(command: str) -> subprocess.Popen:
    return subprocess.Popen(
        [_POWERSHELL, "-NoProfile", "-Command", command],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )


def _run_watch(victim_pid: int, log) -> subprocess.Popen:
    return subprocess.Popen(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", WATCHER,
         "-WatchPid", str(victim_pid), "-LogPath", str(log)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000,
    )


def test_the_watcher_exists_and_is_ascii():
    assert os.path.isfile(WATCHER)
    raw = open(WATCHER, "rb").read()
    assert all(b < 128 for b in raw), "powershell.exe reads this with the console code page: ASCII only"


@pytest.mark.parametrize("how,expected_code", [("kill", 1), ("exit", 7)])
def test_the_watcher_logs_the_exit_code_of_a_process_that_ended(tmp_path, how, expected_code):
    log = tmp_path / "sup.log"
    victim = _spawn_victim("Start-Sleep -Seconds 120" if how == "kill" else "Start-Sleep -Seconds 4; exit 7")
    watcher = None
    try:
        watcher = _run_watch(victim.pid, log)
        time.sleep(4 if how == "kill" else 1)   # let the watcher open its handle first
        if how == "kill":
            victim.kill()                       # TerminateProcess, exit code 1: what Stop-Process does
        victim.wait(timeout=60)
        watcher.wait(timeout=60)
    finally:
        for p in (victim, watcher):
            if p is not None and p.poll() is None:
                p.kill()
    text = log.read_text(encoding="utf-8-sig")
    assert "supervisor pid %d ended" % victim.pid in text, text
    assert "exit code %d" % expected_code in text, text
    assert "powershell.exe alive" in text, text
    assert "leaving its loop" not in text, \
        "the watcher must not forge the supervisor's own clean-exit record"


def test_the_watcher_gives_up_quietly_on_a_pid_that_is_not_running(tmp_path):
    log = tmp_path / "sup.log"
    r = _run_ps(WATCHER, "-WatchPid", "999999", "-LogPath", str(log), timeout=60)
    assert r.returncode == 0


def test_the_supervisor_starts_its_watcher_after_the_mutex_and_before_the_loop():
    code = _strip_comments(_read(SUPERVISOR))
    assert "supervisor_exit_watch.ps1" in code
    call = code.index("Start-SupervisorExitWatch\n")
    mutex = code.index("Global\\m365-copilot-companion-supervisor")
    loop = code.index("while ($true) {")
    assert mutex < call < loop, "the watcher must start once the mutex is held and before the loop"
