# -*- coding: utf-8 -*-
r"""A live reader stuck at zero connections has its host child ended, not the reader.

THE INCIDENT (measured 2026-11-04, %TEMP%\m365-companion-supervisor.log). The host process
of this PC stayed alive with ZERO relay connections and nothing replaced it for ~7 minutes
because the duplicate-launch guard in Start-TunnelHost waits while a reader
(python -m relay.tunnel_host_log) is alive and inside its reconnect window -- it must never
start a second reader (the 2026-11-02 two-hosts-for-one-tunnel outage).

WHAT IS PINNED HERE
  * the PURE decider (scripts/supervisor_zero_conn_recovery.decide_zero_conn_child_kill):
    no reader -> wait; no known child pid -> wait; an unreadable duration -> wait; inside the
    grace -> wait; a live reader with a known host child past the grace -> kill_child.
    Exercised directly on every platform.
  * the supervisor WIRES it: Start-TunnelHost reaches the decider the cross-language way the
    other gates do (& $Py -c ... supervisor_zero_conn_recovery.decide_zero_conn_child_kill),
    ends ONLY the host child (Stop-TunnelHostChild) and keeps the reader, and adds no literal
    "while ($true) {" to the new code (other tests locate the main loop by that text).
  * on Windows, the extracted PowerShell wrapper agrees with the Python decider across a table
    of (seconds at zero, reader alive, child pid) inputs.

HOW THIS RUNS. The pure table needs no PowerShell. The cross-language table drives the
extracted Invoke-ZeroConnChildKillDecision over a temp driver that puts the worktree's
scripts/ on sys.path and uses the REAL supervisor_zero_conn_recovery.py (so the two cannot
drift); it never dot-sources supervisor.ps1 (forever loop, machine-wide mutex, a live
supervisor on this machine).
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

import supervisor_zero_conn_recovery as Z  # noqa: E402

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

#: (seconds_at_zero, reader_alive, child_pid) -> expected word.
_TABLE = [
    (None, False, 0, "wait"),        # no reader: nothing to recover around here
    (999, False, 1234, "wait"),      # no reader even with a stray duration/pid
    (999, True, None, "wait"),       # alive but no known host child -> cannot kill one
    (999, True, 0, "wait"),          # alive, child pid 0 is "none"
    (None, True, 1234, "wait"),      # alive with a child, duration unknown -> never kill
    (0, True, 1234, "wait"),         # alive, just dropped
    (149, True, 1234, "wait"),       # at the four-tick debounce, still reconnecting
    (240, True, 1234, "wait"),       # exactly at the grace: not yet past it
    (241, True, 1234, "kill_child"), # past the measured grace -> end only the host child
    (600, True, 1234, "kill_child"), # long wedged
    ("not-a-number", True, 1234, "wait"),  # unreadable duration while alive -> wait
]


@pytest.mark.parametrize("secs,alive,child,expected", _TABLE)
def test_the_pure_decider(secs, alive, child, expected):
    assert Z.decide_zero_conn_child_kill(secs, alive, child) == expected


def test_the_grace_is_the_measured_one():
    # 240s is the published constant, justified in the module docstring by the measured
    # 2026-11-04 numbers (157s longest reconnect, ~149s debounce, ~7min gap). A change to it
    # is a change to that justification, caught here.
    assert Z.ZERO_CONN_GRACE_SECONDS == 240
    assert Z.decide_zero_conn_child_kill(240, True, 1234) == "wait"
    assert Z.decide_zero_conn_child_kill(241, True, 1234) == "kill_child"


def test_the_grace_is_at_least_the_reader_replacement_window():
    # Ending just the host child is the gentler first move than tearing the reader tree down,
    # so it must be given at least as much room as the reader-replacement window.
    import supervisor_host_launch as H
    assert Z.ZERO_CONN_GRACE_SECONDS >= H.RECONNECT_GRACE_SECONDS


def test_a_custom_grace_is_honoured():
    assert Z.decide_zero_conn_child_kill(30, True, 1234, grace=10) == "kill_child"
    assert Z.decide_zero_conn_child_kill(5, True, 1234, grace=10) == "wait"
    # A bad grace falls back to the default, so a live, wedged, child-owning reader past the
    # default is still kill_child.
    assert Z.decide_zero_conn_child_kill(999, True, 1234, grace="not-a-number") == "kill_child"


# -- every platform: the supervisor wires it --------------------------------------------------

def test_start_tunnel_host_reaches_the_zero_conn_decider():
    src = _read(SUPERVISOR_PS1)
    assert "supervisor_zero_conn_recovery.decide_zero_conn_child_kill(" in src, \
        "Start-TunnelHost must reach the pure decider the cross-language way (& $Py -c ...), so " \
        "tools/unreached.py credits the caller and the two cannot drift"
    assert "Invoke-ZeroConnChildKillDecision" in src


def test_start_tunnel_host_ends_only_the_child_and_keeps_the_reader():
    src = _read(SUPERVISOR_PS1)
    block = _extract_braced_block(src, "function Start-TunnelHost")
    assert "Get-TunnelReaderChildPid" in block, \
        "Start-TunnelHost must find the reader's host child before ending it"
    assert "Stop-TunnelHostChild" in block, \
        "a zero-connection recovery must end ONLY the host child, not the reader tree"
    # It must NOT reach for the reader-tree teardown inside the wait branch.
    wait_idx = block.index('if ($decision -eq "wait")')
    wait_block = _extract_braced_block(block[wait_idx:], 'if ($decision -eq "wait")')
    assert "Stop-TunnelReaderTree" not in wait_block, \
        "the wait branch must never tear down the reader -- only the host child is ended"


def test_stop_tunnel_host_child_does_not_touch_the_reader():
    # Stop-TunnelHostChild ends a devtunnel child only; it must not stop the reader pid.
    src = _read(SUPERVISOR_PS1)
    block = _code_only(_extract_braced_block(src, "function Stop-TunnelHostChild"))
    assert "Name='devtunnel.exe'" in block
    # The reader is a python.exe; the child-kill must be scoped to devtunnel.exe only.
    assert "python" not in block.lower()


def test_no_literal_while_true_in_the_new_functions():
    # Other tests locate the main loop by the literal "while ($true) {"; new supervisor code must
    # use for (;;) instead. Checked over every function this change introduced.
    src = _read(SUPERVISOR_PS1)
    for fn in ("function Get-TunnelReaderChildPid", "function Invoke-ZeroConnChildKillDecision",
               "function Stop-TunnelHostChild"):
        block = _code_only(_extract_braced_block(src, fn))
        assert "while ($true) {" not in block, ("%s must use for (;;), not while ($true)" % fn)


# -- Windows: the PowerShell wrapper agrees with the Python decider ---------------------------

_DRIVER_HEAD = r'''param([string]$RepoScripts)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $RepoScripts
$Py = "python"
function Write-Log($msg) { }
'''


def _build_driver(work, src):
    decider = _extract_braced_block(src, "function Invoke-ZeroConnChildKillDecision")
    body = r'''
$cases = @(
  @{ Secs = $null; Alive = $false; Child = 0 },
  @{ Secs = 999;   Alive = $true;  Child = $null },
  @{ Secs = $null; Alive = $true;  Child = 1234 },
  @{ Secs = 149;   Alive = $true;  Child = 1234 },
  @{ Secs = 240;   Alive = $true;  Child = 1234 },
  @{ Secs = 241;   Alive = $true;  Child = 1234 },
  @{ Secs = 600;   Alive = $true;  Child = 1234 }
)
foreach ($c in $cases) {
  $d = Invoke-ZeroConnChildKillDecision -SecondsAtZero $c.Secs -ReaderAlive $c.Alive -ChildPid $c.Child
  $s = if ($null -eq $c.Secs) { "None" } else { [string]$c.Secs }
  $p = if ($null -eq $c.Child) { "None" } else { [string]$c.Child }
  Write-Output ("$s|$($c.Alive)|$p|$d")
}
'''
    driver = os.path.join(work, "driver.ps1")
    with open(driver, "w", encoding="utf-8") as fh:
        fh.write(_DRIVER_HEAD + "\n" + decider + "\n" + body)
    return driver


def _run(work, src):
    from tools import childproc
    scripts_copy = os.path.join(work, "scripts")
    os.makedirs(scripts_copy, exist_ok=True)
    shutil.copy(os.path.join(REPO, "scripts", "supervisor_zero_conn_recovery.py"), scripts_copy)
    driver = _build_driver(work, src)
    proc = childproc.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver,
         "-RepoScripts", scripts_copy],
        timeout=120, creationflags=childproc.headless_creationflags())
    return proc.stdout.strip().splitlines()


@windows_only
def test_powershell_wrapper_agrees_with_python():
    src = _read(SUPERVISOR_PS1)
    work = tempfile.mkdtemp(prefix="sup_zeroconn_")
    try:
        lines = _run(work, src)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    got = {}
    for ln in lines:
        parts = ln.split("|")
        if len(parts) == 4:
            got[(parts[0], parts[1], parts[2])] = parts[3]
    assert got[("None", "False", "0")] == "wait"
    assert got[("999", "True", "None")] == "wait"
    assert got[("None", "True", "1234")] == "wait"
    assert got[("149", "True", "1234")] == "wait"
    assert got[("240", "True", "1234")] == "wait"
    assert got[("241", "True", "1234")] == "kill_child"
    assert got[("600", "True", "1234")] == "kill_child"
    for (secs_s, alive_s, child_s), word in got.items():
        secs = None if secs_s == "None" else int(secs_s)
        alive = (alive_s == "True")
        child = None if child_s == "None" else int(child_s)
        assert word == Z.decide_zero_conn_child_kill(secs, alive, child), (secs_s, alive_s, child_s, word)
