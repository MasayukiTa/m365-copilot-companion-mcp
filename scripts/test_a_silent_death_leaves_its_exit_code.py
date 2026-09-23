# -*- coding: utf-8 -*-
r"""A server that dies without printing anything must still leave its exit code readable.

ROOT CAUSE (see scripts/supervisor.ps1, Get-ServerExitRecord's and Start-Server's own header
comments for the full account). Start-Server launched the MCP server with `Start-Process
... -RedirectStandardOutput ... -RedirectStandardError ...` and discarded the returned
process object -- no `-PassThru`. Preserving stdout/stderr into *.history.log before each
relaunch works, but a process that dies silently leaves no output BY DEFINITION, so preserved
output can never explain that kind of death: measured 2026-09-16, six exits in two days, every
preserved stderr block held nothing but the uvicorn startup banner, and Windows Error
Reporting logged no fault for any of them. What DOES survive a silent death is the exit code
and the time -- and without `-PassThru` the supervisor threw both away the instant the launch
call returned. It also never said whether an ending was the supervisor's OWN doing (a stale-
code cycle, a kill-by-port relaunch) or the process died on its own, so a routine planned
cycle read exactly like a crash in the log.

This test checks both halves of the fix:

  * SOURCE LEVEL -- both Start-Process calls in Start-Server declare -PassThru, and the
    preserved "=== launch ending ..." history header carries the exit record's facts, not
    just a timestamp.
  * RUNTIME -- the extracted Get-ServerExitRecord function, run against real Windows child
    processes with known exit codes, reports the right decimal AND unsigned-hex exit code,
    honours the planned/unplanned distinction it is handed, and never invents an exit code
    for a process that has not exited.

WHY EXTRACT RATHER THAN DOT-SOURCE. scripts/supervisor.ps1 dot-sources into a script with a
`while ($true)` main loop and an OS-wide single-instance mutex
(`Global\m365-copilot-companion-supervisor`); dot-sourcing the whole file here would either
hang this test or contend with the real supervisor that is running on this machine (see this
repo's live-system warnings). So this test parses the .ps1 TEXT for the
`function Get-ServerExitRecord { ... }` block (balanced braces) and runs just that, plus a
small driver, in an isolated PowerShell process.

Windows-only: supervisor.ps1 itself is Windows-only (WMI CIM queries, devtunnel.exe, Win32
crash-code semantics), and the runtime half of this test launches real Windows child processes
through PowerShell's own Start-Process -PassThru -- the exact call this test is checking.
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


# ── extracting the function under test from the live .ps1, without dot-sourcing it ─────────

def _extract_braced_block(text: str, start_marker: str) -> str:
    """The balanced-brace block starting at the first '{' at/after `start_marker`.

    NOT PowerShell-aware: it does not know about strings or here-strings, so an unbalanced
    '{'/'}' pair inside a string literal within the block would fool it. There isn't one in
    the functions this test extracts (checked by hand): every brace pair inside a string
    literal in Get-ServerExitRecord / Start-Server -- e.g. "0x{0:X8}" -f ..., "{0}h{1}m" -f
    ... -- is itself balanced, so naive counting still lands on the right closing brace.
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
    """Drop every line that is, after leading whitespace, a '#' comment.

    _srcprobe-style care (see tests/_srcprobe.py) applied to PowerShell, which this repo has
    no AST for: a raw-text search over a heavily-commented file like supervisor.ps1 can match
    a COMMENT that mentions a pattern by name rather than the code doing it -- this very
    file's own header comments say "-PassThru" and "launch ending" in prose. Stripping whole
    comment lines first keeps the assertions below about what runs. Inline trailing comments
    are left alone on purpose: none of the lines these checks look at have one, and doing more
    than "drop comment lines" would mean parsing PowerShell, which this is not.
    """
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


@pytest.fixture(scope="module")
def supervisor_source() -> str:
    with open(SUPERVISOR_PS1, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def exit_record_function(supervisor_source: str) -> str:
    return _extract_braced_block(supervisor_source, "function Get-ServerExitRecord")


@pytest.fixture(scope="module")
def start_server_code_only(supervisor_source: str) -> str:
    block = _extract_braced_block(supervisor_source, "function Start-Server")
    return _strip_ps_comment_lines(block)


def _venv_python() -> str:
    candidate = os.path.join(REPO, ".venv", "Scripts", "python.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


# ── source-level checks ─────────────────────────────────────────────────────────────────────

def test_both_start_process_calls_in_start_server_use_passthru(start_server_code_only):
    """The write side of the fix. Without -PassThru there is no process object to read from,
    and Get-ServerExitRecord's entire premise -- that the exit code survives -- is false."""
    idxs = [m.start() for m in
            re.finditer(r"Start-Process\s+-FilePath\s+\$Py\b", start_server_code_only)]
    assert len(idxs) == 2, (
        "expected exactly 2 Start-Process calls launching main.py in Start-Server (the "
        "redirected launch and its no-redirection fallback), found %d" % len(idxs)
    )
    for idx in idxs:
        window = start_server_code_only[idx:idx + 260]
        assert "-PassThru" in window, (
            "a Start-Process call in Start-Server does not declare -PassThru:\n%s" % window
        )


def test_history_header_carries_the_exit_record(start_server_code_only):
    """The '=== launch ending ...' header must say WHY the launch ended, not only WHEN --
    otherwise a preserved block that holds no output (a silent death) is indistinguishable
    from one preserved because the supervisor deliberately cycled a healthy server."""
    idx = start_server_code_only.index("=== launch ending ")
    window = start_server_code_only[idx:idx + 200]
    assert "$exitRecord" in window, (
        "the '=== launch ending ...' history header no longer carries the exit record:\n%s"
        % window
    )


def test_stale_cycle_declares_its_reason_before_relaunching(supervisor_source):
    """Invoke-StaleServerCycle is one of the two places this supervisor stops the server on
    purpose (the other is the kill-by-port/kill-stale-instances code inside Start-Server
    itself). It must say why BEFORE calling Start-Server, or its own relaunch would fall back
    to Start-Server's generic default reason and lose the specific "stale code cycle" label."""
    block = _strip_ps_comment_lines(
        _extract_braced_block(supervisor_source, "function Invoke-StaleServerCycle"))
    reason_idx = block.index("$script:ServerPlannedEndReason")
    call_idx = block.index("Start-Server", reason_idx)
    assert call_idx > reason_idx
    assert "stale code cycle" in block[reason_idx:call_idx]


# ── runtime checks: the extracted function against real child processes ────────────────────

_DRIVER_HEADER = '''param(
    [Parameter(Mandatory=$true)][string]$PyExe,
    [Parameter(Mandatory=$true)][string]$OutFile
)

$ErrorActionPreference = "Stop"
'''

# Deliberately a plain (non-f, non-.format) string: it is concatenated around the extracted
# PowerShell function text, which is full of literal '{'/'}' that must NOT be interpreted as
# Python format placeholders.
_DRIVER_FOOTER = r'''
function New-KnownChild([string]$PyCode) {
    # QUOTED AS ONE ARGUMENT, NOT PASSED AS SEPARATE -ArgumentList ELEMENTS. Start-Process
    # -ArgumentList '-c','import os; os._exit(7)' was tried first and silently produced
    # ExitCode=1 for every child regardless of the requested code -- PowerShell re-joins the
    # array with spaces without quoting the piece that itself contains spaces, so Python saw
    # a mangled argv and errored. A single pre-quoted string round-trips correctly.
    $argStr = '-c "' + $PyCode + '"'
    return Start-Process -FilePath $PyExe -ArgumentList $argStr -WindowStyle Hidden -PassThru
}

$results = [ordered]@{}

try {
    # A crash-looking exit code, PLANNED -- as if the supervisor itself decided to cycle it
    # (e.g. Invoke-StaleServerCycle). 0xC0000005 is a real Windows access-violation code, not
    # a made-up one, so the hex/unsigned-conversion path is exercised for real.
    $launch1 = Get-Date
    $p1 = New-KnownChild 'import os; os._exit(-1073741819)'
    $p1.WaitForExit()
    $results.planned_crash = Get-ServerExitRecord -Process $p1 -LaunchTime $launch1 -PlannedReason "stale code cycle"

    # The SAME crash-looking exit code, UNPLANNED -- this is the scenario the whole feature
    # exists for: a server that vanished on its own, with nobody having declared a reason.
    $launch2 = Get-Date
    $p2 = New-KnownChild 'import os; os._exit(-1073741819)'
    $p2.WaitForExit()
    $results.unplanned_crash = Get-ServerExitRecord -Process $p2 -LaunchTime $launch2 -PlannedReason $null

    # A small, unambiguous decimal exit code, so decimal correctness is checked independent
    # of the negative/unsigned-hex conversion path exercised by the crash code above.
    $launch3 = Get-Date
    $p3 = New-KnownChild 'import os; os._exit(7)'
    $p3.WaitForExit()
    $results.unplanned_seven = Get-ServerExitRecord -Process $p3 -LaunchTime $launch3 -PlannedReason $null

    # A still-running child must report no exit code at all -- inventing one for a live
    # process would be worse than reporting nothing.
    $launch4 = Get-Date
    $p4 = New-KnownChild 'import time; time.sleep(30)'
    try {
        Start-Sleep -Milliseconds 400
        $results.still_running = Get-ServerExitRecord -Process $p4 -LaunchTime $launch4 -PlannedReason $null
    } finally {
        # Cleaned up here, unconditionally, regardless of what the check above found.
        try { Stop-Process -Id $p4.Id -Force -ErrorAction SilentlyContinue } catch { }
    }

    # No process object at all -- first launch after a supervisor start, or a server this
    # supervisor did not launch. Must say so plainly, never invent a record.
    $results.no_record = Get-ServerExitRecord -Process $null -LaunchTime $null -PlannedReason $null
}
finally {
    # finally, not the happy path: if anything above threw, we still want to see how far it
    # got rather than a bare non-zero exit code with no explanation.
    $results | ConvertTo-Json -Depth 6 | Set-Content -Path $OutFile -Encoding UTF8
}
'''


def _run_driver(exit_record_function: str, py_exe: str, work_dir: str) -> dict:
    """Write the extracted function plus a small driver to a temp .ps1, run it, return the
    parsed JSON result. All child processes the driver starts are cleaned up inside the
    driver's own try/finally (see _DRIVER_FOOTER) -- this function never holds a live handle
    to any of them itself, since childproc.run blocks until the whole driver has exited."""
    driver_path = os.path.join(work_dir, "driver.ps1")
    out_path = os.path.join(work_dir, "out.json")
    driver_text = _DRIVER_HEADER + "\n\n" + exit_record_function + "\n\n" + _DRIVER_FOOTER
    with open(driver_path, "w", encoding="utf-8") as fh:
        fh.write(driver_text)

    proc = childproc.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver_path,
         "-PyExe", py_exe, "-OutFile", out_path],
        timeout=90,
        creationflags=childproc.headless_creationflags(),
    )
    assert proc.returncode == 0, (
        "driver powershell exited %s\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (proc.returncode, proc.stdout, proc.stderr)
    )
    assert os.path.isfile(out_path), "driver did not write its output file: %s" % out_path
    # -Encoding UTF8 in Windows PowerShell 5.1 writes a BOM; utf-8-sig strips it rather than
    # leaving a stray U+FEFF at the front of the first key.
    with open(out_path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def driver_result(exit_record_function, tmp_path_factory) -> dict:
    work_dir = str(tmp_path_factory.mktemp("silent_death_driver"))
    return _run_driver(exit_record_function, _venv_python(), work_dir)


def test_planned_crash_reports_decimal_and_hex_and_is_marked_planned(driver_result):
    rec = driver_result["planned_crash"]
    assert rec["HasExited"] is True
    assert rec["ExitCode"] == -1073741819
    assert rec["ExitCodeHex"] == "0xC0000005"
    assert rec["Planned"] is True
    assert rec["PlannedReason"] == "stale code cycle"
    assert rec["Summary"] == "planned: stale code cycle"


def test_unplanned_crash_reports_the_same_code_but_is_marked_unplanned(driver_result):
    """The scenario the whole feature exists for: the exact same crash code, but nobody
    declared a reason -- because nobody caused it. The planned flag is the only thing that
    tells this apart from test_planned_crash_..., and it must come out false here."""
    rec = driver_result["unplanned_crash"]
    assert rec["HasExited"] is True
    assert rec["ExitCode"] == -1073741819
    assert rec["ExitCodeHex"] == "0xC0000005"
    assert rec["Planned"] is False
    assert rec["Summary"] == "unplanned"


def test_small_decimal_exit_code_is_exact(driver_result):
    rec = driver_result["unplanned_seven"]
    assert rec["HasExited"] is True
    assert rec["ExitCode"] == 7
    assert rec["ExitCodeHex"] == "0x00000007"
    assert rec["Planned"] is False


def test_still_running_process_reports_no_exit_code(driver_result):
    rec = driver_result["still_running"]
    assert rec["HasExited"] is False
    assert rec["ExitCode"] is None
    assert rec["ExitCodeHex"] is None
    assert rec["Summary"] == "still running, replaced"


def test_no_process_object_says_so_rather_than_inventing_a_record(driver_result):
    rec = driver_result["no_record"]
    assert rec["ServerPid"] is None
    assert rec["HasExited"] is None
    assert rec["ExitCode"] is None
    assert rec["Summary"] == "no record: not launched by this supervisor"
