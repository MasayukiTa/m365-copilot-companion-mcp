# -*- coding: utf-8 -*-
r"""The supervisor replaces its OWN Dev Tunnel host when it is alive but never reconnects.

THE INCIDENT (measured 2026-11-04, %TEMP%\m365-companion-supervisor.log and
.fleet\tunnel_host.json). At 03:46:30 the supervisor logged "tunnel host connections = 0
(1/4)"; at 03:49:00 it reached 4/4 and logged "reader pid 16680 still running, host
reconnecting (waited 0 s)" (and the same for pid 26832) -- the J6 rule: never start a second
reader while the first is alive. But the OLD devtunnel child (pid 8304, parent 26832) was
alive with NO relay connection, and nothing replaced it for 7 minutes (03:46-03:53); external
/health through the tunnel URL timed out that whole period. It was stopped by hand at 03:53:19
and the tunnel was back at 03:56:33.

So J6 (one reader) + "reader alive" can wait forever on a host that will never reconnect:
decide_host_launch's 180s grace bounds the READER-wait, not a devtunnel child that is alive but
disconnected.

WHAT IS PINNED HERE
  * the PURE decider (scripts/supervisor_host_launch.decide_child_strike): no reader -> wait;
    no identifiable child of ours -> wait; an unreadable duration -> wait; a reader alive with a
    known child and 0 connections for longer than CHILD_RELAY_GRACE_SECONDS (600s) -> strike.
    Exercised directly on every platform.
  * the supervisor WIRES it: Stop-StuckTunnelChild reaches the decider the cross-language way the
    other gates do (& $Py -c ... supervisor_host_launch.decide_child_strike), stops ONLY the
    child of our reader (Get-OurReaderDevtunnelChildPid, same parent+command-line scope as
    Stop-TunnelReaderTree), never the reader itself and never a second reader, and uses no
    literal "while ($true) {" in the new code (other tests locate the main loop by that text).
  * on Windows, the extracted PowerShell wrapper agrees with the Python decider across a table.

HOW THIS RUNS. The pure table needs no PowerShell. The cross-language table drives the extracted
Invoke-ChildStrikeDecision over a temp driver that puts the worktree's scripts/ on sys.path and
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

#: (seconds_with_zero_connections, reader_alive, child_pid) -> expected word.
_TABLE = [
    (None, False, 1234, "wait"),       # no reader: decide_host_launch owns the replace path
    (9999, False, 1234, "wait"),       # no reader, stray duration ignored
    (9999, True, None, "wait"),        # alive, no identifiable child of ours -> nothing to strike
    (9999, True, 0, "wait"),           # alive, child pid 0 -> nothing to strike
    (None, True, 1234, "wait"),        # alive, child known, duration unknown -> never strike
    (0, True, 1234, "wait"),           # alive, just dropped
    (420, True, 1234, "wait"),         # alive, at the measured worst-case in-place recovery
    (600, True, 1234, "wait"),         # alive, exactly at the window: not yet past it
    (601, True, 1234, "strike"),       # alive, child known, past the 600s window -> strike it
    (3600, True, 1234, "strike"),      # alive, long wedged
    ("not-a-number", True, 1234, "wait"),  # unreadable duration while alive -> wait
]


@pytest.mark.parametrize("secs,alive,child,expected", _TABLE)
def test_the_pure_decider(secs, alive, child, expected):
    assert H.decide_child_strike(secs, alive, child) == expected


def test_the_threshold_is_the_measured_one():
    # 600s is the published constant, justified in the module by the measured 420s worst-case
    # in-place reconnect (and the ~90s normal child-exit re-host). A change to it is a change to
    # that justification, caught here.
    assert H.CHILD_RELAY_GRACE_SECONDS == 600
    assert H.decide_child_strike(600, True, 1234) == "wait"
    assert H.decide_child_strike(601, True, 1234) == "strike"


def test_the_grace_is_above_the_reader_wait_grace():
    # The child-strike window must sit ABOVE decide_host_launch's reader-wait grace: the reader's
    # own recovery (up to the 420s worst case) has to be allowed to finish first, and 180s < 600s.
    assert H.CHILD_RELAY_GRACE_SECONDS > H.RECONNECT_GRACE_SECONDS


def test_a_custom_grace_is_honoured():
    assert H.decide_child_strike(30, True, 1234, grace=10) == "strike"
    assert H.decide_child_strike(5, True, 1234, grace=10) == "wait"
    assert H.decide_child_strike(5000, True, 1234, grace="not-a-number") == "strike"  # bad grace -> default 600
    assert H.decide_child_strike(5, True, 1234, grace="not-a-number") == "wait"


# -- every platform: the supervisor wires it --------------------------------------------------

def test_stop_stuck_child_reaches_the_pure_decider():
    src = _read(SUPERVISOR_PS1)
    assert "supervisor_host_launch.decide_child_strike(" in src, \
        "Stop-StuckTunnelChild must reach the pure decider the cross-language way (& $Py -c ...), " \
        "so tools/unreached.py credits the caller and the two cannot drift"
    assert "Invoke-ChildStrikeDecision" in src
    assert "Stop-StuckTunnelChild" in src


def test_stop_stuck_child_stops_only_the_child_not_the_reader():
    src = _read(SUPERVISOR_PS1)
    # CODE ONLY: the comments legitimately mention Start-TunnelHost / the reader by name; what
    # must be absent is a CALL to them in the executable body.
    block = _code_only(_extract_braced_block(src, "function Stop-StuckTunnelChild"))
    # It stops the child pid the discovery returned...
    assert "Stop-Process -Id $childPid" in block, \
        "Stop-StuckTunnelChild must stop the identified child pid"
    # ...and must NOT tear down the reader tree or launch another reader.
    assert "Stop-TunnelReaderTree" not in block, \
        "a child strike must NOT stop the reader itself -- the reader exits on its own"
    assert "Start-TunnelHost" not in block, \
        "a child strike must NOT start a second reader"


def test_the_child_is_scoped_to_our_reader():
    # The child discovery must be scoped to a child of OUR reader with a host command line for
    # THIS tunnel -- the same parent + Test-IsTunnelHostCommandLine scope Stop-TunnelReaderTree
    # uses -- so a user's `devtunnel login` or an unrelated tunnel is never struck.
    src = _read(SUPERVISOR_PS1)
    block = _extract_braced_block(src, "function Get-OurReaderDevtunnelChildPid")
    assert "Get-ExistingTunnelReaders" in block
    assert "ParentProcessId" in block
    assert "Test-IsTunnelHostCommandLine" in block


def test_the_strike_is_wired_into_the_hosting_check():
    # The tick must call Stop-StuckTunnelChild right after it updates the relay gap, so the
    # decision runs every tunnel-hosting tick on a current duration.
    src = _read(SUPERVISOR_PS1)
    block = _extract_braced_block(src, "function Invoke-TunnelHostingCheck")
    assert "Update-ReaderRelayGap" in block
    assert "Stop-StuckTunnelChild" in block


def test_no_literal_while_true_in_the_new_supervisor_functions():
    # Other tests locate the main loop by the literal "while ($true) {"; new supervisor code must
    # not add another. Checked over every function this change introduced.
    src = _read(SUPERVISOR_PS1)
    for fn in ("function Stop-StuckTunnelChild", "function Get-OurReaderDevtunnelChildPid",
               "function Invoke-ChildStrikeDecision", "function Test-ChildStrikeAllowed"):
        block = _code_only(_extract_braced_block(src, fn))
        assert "while ($true) {" not in block, ("%s must use for (;;), not while ($true)" % fn)


# -- Windows: the PowerShell wrapper agrees with the Python decider ----------------------------

_DRIVER_HEAD = r'''param([string]$RepoScripts)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $RepoScripts
$Py = "python"
function Write-Log($msg) { }
'''


def _build_driver(work, src, body):
    decider = _extract_braced_block(src, "function Invoke-ChildStrikeDecision")
    driver = os.path.join(work, "driver.ps1")
    with open(driver, "w", encoding="utf-8") as fh:
        fh.write(_DRIVER_HEAD + "\n" + decider + "\n" + body)
    return driver


def _run(work, src, body):
    from tools import childproc
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
    work = tempfile.mkdtemp(prefix="sup_childstrike_")
    try:
        body = r'''
$cases = @(
  @{ Secs = $null; Alive = $false; Child = 1234 },
  @{ Secs = 9999; Alive = $true;  Child = $null },
  @{ Secs = 9999; Alive = $true;  Child = 0 },
  @{ Secs = $null; Alive = $true; Child = 1234 },
  @{ Secs = 0;    Alive = $true;  Child = 1234 },
  @{ Secs = 600;  Alive = $true;  Child = 1234 },
  @{ Secs = 601;  Alive = $true;  Child = 1234 },
  @{ Secs = 3600; Alive = $true;  Child = 1234 }
)
foreach ($c in $cases) {
  $d = Invoke-ChildStrikeDecision -SecondsWithZeroConnections $c.Secs -ReaderAlive $c.Alive -ChildPid $c.Child
  $s = if ($null -eq $c.Secs) { "None" } else { [string]$c.Secs }
  $p = if ($null -eq $c.Child) { "None" } else { [string]$c.Child }
  Write-Output ("$s|$($c.Alive)|$p|$d")
}
'''
        lines = _run(work, src, body)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    got = {}
    for ln in lines:
        parts = ln.split("|")
        if len(parts) == 4:
            got[(parts[0], parts[1], parts[2])] = parts[3]
    assert got[("None", "False", "1234")] == "wait"
    assert got[("9999", "True", "None")] == "wait"
    assert got[("9999", "True", "0")] == "wait"
    assert got[("None", "True", "1234")] == "wait"
    assert got[("0", "True", "1234")] == "wait"
    assert got[("600", "True", "1234")] == "wait"
    assert got[("601", "True", "1234")] == "strike"
    assert got[("3600", "True", "1234")] == "strike"
    for (secs_s, alive_s, child_s), word in got.items():
        secs = None if secs_s == "None" else int(secs_s)
        alive = (alive_s == "True")
        child = None if child_s == "None" else int(child_s)
        assert word == H.decide_child_strike(secs, alive, child), (secs_s, alive_s, child_s, word)
