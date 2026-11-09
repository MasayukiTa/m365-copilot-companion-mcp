# -*- coding: utf-8 -*-
r"""The supervisor never runs two Dev Tunnel host readers at once.

THE INCIDENT (measured 2026-11-02, %TEMP%\m365-companion-supervisor.log and
.setup\logs\devtunnel-host-ts.log). A <1 min outbound network drop at 07:34 made the reader
started at 21:23 (python -m relay.tunnel_host_log, a supervisor child) lose its relay
connection; it was retrying with its own backoff. The supervisor's "tunnel host connections =
0 (4/4)" rule fired at 07:34:52 and Start-TunnelHost launched a SECOND reader WITHOUT stopping
the first. Two devtunnel hosts for one tunnel took each other's relay session ("Connection
lost" in a loop) and the gateway answered 502/timeouts from ~07:34 to ~07:41.

WHAT IS PINNED HERE
  * the PURE decider (scripts/supervisor_host_launch.decide_host_launch): no reader -> replace;
    a reader alive and inside the 180s reconnect window -> wait; alive but without a relay for
    longer than 180s -> replace; an unreadable duration while alive -> wait (never race its own
    recovery). Exercised directly on every platform.
  * the supervisor WIRES it: Start-TunnelHost reaches the decider the cross-language way the
    other gates do (& $Py -c ... supervisor_host_launch.decide_host_launch), logs the required
    "reader pid N still running, host reconnecting (waited S s)" line on a wait, and uses no
    literal "while ($true) {" in the new code (other tests locate the main loop by that text).
  * on Windows, the extracted PowerShell helpers agree with the Python decider across a table of
    (reader alive, seconds without relay) inputs, and Update-ReaderRelayGap / 
    Get-ReaderSecondsWithoutRelay track the gap: a relay connection clears it, a 0-count while a
    reader is alive opens it.

HOW THIS RUNS. The pure table needs no PowerShell. The cross-language table drives the extracted
Invoke-HostLaunchDecision over a temp driver that puts the worktree's scripts/ on sys.path and
uses the REAL supervisor_host_launch.py (so the two cannot drift); it never dot-sources
supervisor.ps1 (forever loop, machine-wide mutex, a live supervisor on this machine).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import supervisor_host_launch as H  # noqa: E402

SUPERVISOR_PS1 = os.path.join(REPO, "scripts", "supervisor.ps1")

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


# -- every platform: the pure decider ---------------------------------------------------------

#: (reader_alive, seconds_without_relay) -> expected word.
_TABLE = [
    (False, None, "replace"),       # no reader: launch exactly one
    (False, 999, "replace"),        # no reader, stray duration ignored
    (True, None, "wait"),           # alive, duration unknown -> never race its recovery
    (True, 0, "wait"),              # alive, just dropped
    (True, 60, "wait"),            # alive, short drop recovers in ~1 min
    (True, 179, "wait"),           # alive, just under the window
    (True, 180, "wait"),           # alive, exactly at the window: not yet past it
    (True, 181, "replace"),        # alive, past the measured 7-minute-worst-case window
    (True, 600, "replace"),        # alive, long wedged
    (True, "not-a-number", "wait"),  # unreadable duration while alive -> wait
]


@pytest.mark.parametrize("alive,secs,expected", _TABLE)
def test_the_pure_decider(alive, secs, expected):
    assert H.decide_host_launch(alive, secs) == expected


def test_the_threshold_is_the_measured_one():
    # 180s is the published constant, justified in the module docstring by the measured 7-minute
    # worst-case reconnect. A change to it is a change to that justification, caught here.
    assert H.RECONNECT_GRACE_SECONDS == 180
    assert H.decide_host_launch(True, 180) == "wait"
    assert H.decide_host_launch(True, 181) == "replace"


def test_a_custom_grace_is_honoured():
    assert H.decide_host_launch(True, 30, grace=10) == "replace"
    assert H.decide_host_launch(True, 5, grace=10) == "wait"
    assert H.decide_host_launch(True, 50, grace="not-a-number") == "wait"  # bad grace -> default 180


# -- every platform: the supervisor wires it --------------------------------------------------

def test_start_tunnel_host_reaches_the_pure_decider():
    src = _read(SUPERVISOR_PS1)
    assert "supervisor_host_launch.decide_host_launch(" in src, \
        "Start-TunnelHost must reach the pure decider the cross-language way (& $Py -c ...), so " \
        "tools/unreached.py credits the caller and the two cannot drift"
    assert "Invoke-HostLaunchDecision" in src


def test_start_tunnel_host_guards_against_a_second_reader():
    src = _read(SUPERVISOR_PS1)
    block = _extract_braced_block(src, "function Start-TunnelHost")
    assert "Get-ExistingTunnelReaders" in block, \
        "Start-TunnelHost must look for an existing reader before launching"
    assert "Stop-TunnelReaderTree" in block, \
        "a replace must stop the reader tree (reader + its devtunnel child) first"
    # The exact wait log line the GOAL specifies (pid and waited-seconds are interpolated).
    assert 'still running, host reconnecting (waited' in block


def test_no_literal_while_true_in_the_new_supervisor_functions():
    # Other tests locate the main loop by the literal "while ($true) {"; new supervisor code must
    # not add another. Checked over every function this change introduced.
    src = _read(SUPERVISOR_PS1)
    for fn in ("function Start-TunnelHost", "function Update-ReaderRelayGap",
               "function Get-ReaderSecondsWithoutRelay", "function Get-ExistingTunnelReaders",
               "function Stop-TunnelReaderTree", "function Invoke-HostLaunchDecision"):
        block = _code_only(_extract_braced_block(src, fn))
        assert "while ($true) {" not in block, ("%s must use for (;;), not while ($true)" % fn)


# -- Windows: the PowerShell helpers agree with the Python decider ----------------------------

_DRIVER_HEAD = r'''param([string]$RepoScripts)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $RepoScripts
$Py = "python"
function Write-Log($msg) { }
function Get-BareTunnelName($n) { return ($n -replace '\.[a-z0-9]+$', '') }
'''


def _build_driver(work, src, body):
    decider = _extract_braced_block(src, "function Invoke-HostLaunchDecision")
    gap_update = _extract_braced_block(src, "function Update-ReaderRelayGap")
    gap_read = _extract_braced_block(src, "function Get-ReaderSecondsWithoutRelay")
    driver = os.path.join(work, "driver.ps1")
    with open(driver, "w", encoding="utf-8") as fh:
        fh.write(_DRIVER_HEAD + "\n$script:ReaderNoRelaySince = $null\n"
                 + decider + "\n" + gap_update + "\n" + gap_read + "\n" + body)
    return driver


def _run(work, src, body):
    from tools import childproc
    # The driver puts the WORKTREE's scripts/ on sys.path via $Root\scripts, so the REAL
    # supervisor_host_launch.py is used -- the point of the cross-language check.
    scripts_copy = os.path.join(work, "scripts")
    os.makedirs(scripts_copy, exist_ok=True)
    shutil.copy(os.path.join(REPO, "scripts", "supervisor_host_launch.py"), scripts_copy)
    driver = _build_driver(work, src, body)
    proc = childproc.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver,
         "-RepoScripts", scripts_copy],
        timeout=120, creationflags=childproc.headless_creationflags())
    return proc.stdout.strip().splitlines()


@windows_only
def test_powershell_decider_agrees_with_python():
    src = _read(SUPERVISOR_PS1)
    work = tempfile.mkdtemp(prefix="sup_hostlaunch_")
    try:
        body = r'''
$cases = @(
  @{ Alive = $false; Secs = $null },
  @{ Alive = $true;  Secs = $null },
  @{ Alive = $true;  Secs = 60 },
  @{ Alive = $true;  Secs = 180 },
  @{ Alive = $true;  Secs = 181 },
  @{ Alive = $true;  Secs = 600 }
)
foreach ($c in $cases) {
  $d = Invoke-HostLaunchDecision -ReaderAlive $c.Alive -SecondsWithoutRelay $c.Secs
  $s = if ($null -eq $c.Secs) { "None" } else { [string]$c.Secs }
  Write-Output ("$($c.Alive)|$s|$d")
}
'''
        lines = _run(work, src, body)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    got = {}
    for ln in lines:
        parts = ln.split("|")
        if len(parts) == 3:
            got[(parts[0], parts[1])] = parts[2]
    # The PowerShell wrapper must return exactly what the Python decider returns for each case.
    assert got[("False", "None")] == "replace"
    assert got[("True", "None")] == "wait"
    assert got[("True", "60")] == "wait"
    assert got[("True", "180")] == "wait"
    assert got[("True", "181")] == "replace"
    assert got[("True", "600")] == "replace"
    for (alive_s, secs_s), word in got.items():
        alive = (alive_s == "True")
        secs = None if secs_s == "None" else int(secs_s)
        assert word == H.decide_host_launch(alive, secs), (alive_s, secs_s, word)


@windows_only
def test_the_relay_gap_tracking():
    src = _read(SUPERVISOR_PS1)
    work = tempfile.mkdtemp(prefix="sup_hostgap_")
    try:
        body = r'''
# A relay connection clears the gap.
Update-ReaderRelayGap -Connections 1 -ReaderAlive $true
Write-Output ("after_relay=" + $(if ($null -eq (Get-ReaderSecondsWithoutRelay)) { "null" } else { "set" }))
# 0 connections while a reader is alive opens the gap.
Update-ReaderRelayGap -Connections 0 -ReaderAlive $true
Write-Output ("after_zero=" + $(if ($null -eq (Get-ReaderSecondsWithoutRelay)) { "null" } else { "set" }))
# No reader alive clears it again.
Update-ReaderRelayGap -Connections 0 -ReaderAlive $false
Write-Output ("after_noreader=" + $(if ($null -eq (Get-ReaderSecondsWithoutRelay)) { "null" } else { "set" }))
'''
        lines = _run(work, src, body)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out = dict(l.split("=", 1) for l in lines if "=" in l)
    assert out.get("after_relay") == "null", lines
    assert out.get("after_zero") == "set", lines
    assert out.get("after_noreader") == "null", lines
