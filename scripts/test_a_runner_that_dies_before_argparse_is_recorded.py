# -*- coding: utf-8 -*-
r"""A fleet/review auto-resume runner that dies before it can log anything must still be seen.

ROOT CAUSE (see docs/agent_contract.md, and Invoke-AutoResumeRunnerCheck's own header comment
in scripts/supervisor.ps1). A fleet or review coordinator that "dies before argparse (a bad
path, the wrong interpreter, a failed import)" leaves NO record at all: no marker (that is
written from inside main(), and this death happens before main() runs), no log line, nothing.
docs/agent_contract.md says "nothing covers" this case -- true from INSIDE that process, but
not true of its PARENT. Invoke-FleetAutoResume and Invoke-ReviewAutoResume launch these
runners with `Start-Process ... -m relay.fleet_runner` / `-m bench.review_run` and, before this
fix, discarded the returned process object exactly like Start-Server did for the MCP server
itself (see scripts/test_a_silent_death_leaves_its_exit_code.py). This test proves the fix:

  * SOURCE LEVEL -- both auto-resume launches declare -PassThru and register the process with
    Register-AutoResumeRunner, and Invoke-AutoResumeRunnerCheck is actually wired into the
    main loop (not just defined and never called).
  * RUNTIME -- the extracted tracking functions, run against real child processes standing in
    for "died before argparse" (a failed import, exit 1, instantly) and "a normal quick clean
    exit" (exit 0, instantly) and "still running", produce the right supervisor-log line for
    each: the quick, nonzero-code death is flagged prominently with the exact reproduce
    command; the clean exit is logged briefly and NOT flagged; the still-running one produces
    no log line at all yet.

WHY EXTRACT RATHER THAN DOT-SOURCE: see scripts/test_a_silent_death_leaves_its_exit_code.py's
identical note -- supervisor.ps1 has a `while ($true)` main loop and a machine-wide mutex, and
a live supervisor is running on this machine right now. This test never dot-sources it; it
parses the .ps1 TEXT for the functions under test (balanced braces) and two script-scope
initializer LINES their state depends on, and runs all of it, plus a small driver with its own
Write-Log stub (so nothing here writes to the real supervisor log), in an isolated PowerShell
process.

Windows-only, for the same reasons as the sibling test file.
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


# ── extracting the functions/state under test from the live .ps1 ───────────────────────────

def _extract_braced_block(text: str, start_marker: str) -> str:
    """The balanced-brace block starting at the first '{' at/after `start_marker`.

    Not PowerShell-aware -- see scripts/test_a_silent_death_leaves_its_exit_code.py's identical
    helper for why naive counting is still correct for the functions this test extracts (every
    brace pair inside a string literal in them is itself balanced).
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


def _extract_line(text: str, needle: str) -> str:
    """The single source line containing `needle`, e.g. a script-scope initializer statement
    that is not itself wrapped in a function and so is not reachable via balanced braces."""
    idx = text.index(needle)
    start = text.rfind("\n", 0, idx) + 1
    end = text.index("\n", idx)
    if end == -1:
        end = len(text)
    return text[start:end]


def _strip_ps_comment_lines(text: str) -> str:
    """Drop every line that is, after leading whitespace, a '#' comment. _srcprobe-style care
    (see tests/_srcprobe.py) applied to PowerShell -- see the sibling test file's identical
    helper for the full reasoning."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


@pytest.fixture(scope="module")
def supervisor_source() -> str:
    with open(SUPERVISOR_PS1, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def supervisor_code_only(supervisor_source: str) -> str:
    return _strip_ps_comment_lines(supervisor_source)


@pytest.fixture(scope="module")
def tracking_ps_fragments(supervisor_source: str) -> dict:
    """Every piece of supervisor.ps1 that Invoke-AutoResumeRunnerCheck's runtime behaviour
    depends on: the exit-record formatter it reuses, the two script-scope state initializers
    (a List and a threshold constant) that live outside any function, and the two functions
    themselves."""
    return {
        "exit_record_function": _extract_braced_block(
            supervisor_source, "function Get-ServerExitRecord"),
        "runners_list_init": _extract_line(
            supervisor_source, "$script:AutoResumeRunners = New-Object"),
        "register_function": _extract_braced_block(
            supervisor_source, "function Register-AutoResumeRunner"),
        "quick_death_threshold_init": _extract_line(
            supervisor_source, "$script:AutoResumeQuickDeathSeconds ="),
        "check_function": _extract_braced_block(
            supervisor_source, "function Invoke-AutoResumeRunnerCheck"),
    }


def _venv_python() -> str:
    candidate = os.path.join(REPO, ".venv", "Scripts", "python.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


# ── source-level checks ─────────────────────────────────────────────────────────────────────

def _function_block_code_only(supervisor_source: str, marker: str) -> str:
    return _strip_ps_comment_lines(_extract_braced_block(supervisor_source, marker))


def test_fleet_auto_resume_registers_a_passthru_launch(supervisor_source):
    block = _function_block_code_only(supervisor_source, "function Invoke-FleetAutoResume")
    idx = block.index("Start-Process -FilePath $Py")
    window = block[idx:idx + 300]
    assert "-PassThru" in window, (
        "Invoke-FleetAutoResume's Start-Process call does not declare -PassThru:\n%s" % window
    )
    assert "Register-AutoResumeRunner" in block, (
        "Invoke-FleetAutoResume launches a runner but never registers it for exit tracking"
    )


def test_review_auto_resume_registers_a_passthru_launch(supervisor_source):
    block = _function_block_code_only(supervisor_source, "function Invoke-ReviewAutoResume")
    idx = block.index("Start-Process -FilePath $Py")
    window = block[idx:idx + 300]
    assert "-PassThru" in window, (
        "Invoke-ReviewAutoResume's Start-Process call does not declare -PassThru:\n%s" % window
    )
    assert "Register-AutoResumeRunner" in block, (
        "Invoke-ReviewAutoResume launches a runner but never registers it for exit tracking"
    )


def test_the_runner_check_is_actually_called_from_the_main_loop(supervisor_code_only):
    """A function that is only ever defined and never called is dead code -- this is the
    whole point of the feature, so it must be wired into the loop, not just written."""
    occurrences = [m.start() for m in
                   re.finditer(r"\bInvoke-AutoResumeRunnerCheck\b", supervisor_code_only)]
    # Exactly one definition ("function Invoke-AutoResumeRunnerCheck {") and at least one
    # bare call site outside that definition.
    assert len(occurrences) >= 2, (
        "Invoke-AutoResumeRunnerCheck appears to be defined but never called from the main "
        "loop (found %d occurrence(s) total, need a definition plus a call)" % len(occurrences)
    )
    def_line_idx = supervisor_code_only.index("function Invoke-AutoResumeRunnerCheck")
    def_idx = def_line_idx + len("function ")   # occurrences[] points at the bare identifier
    call_sites = [i for i in occurrences if i != def_idx]
    assert call_sites, "no call site for Invoke-AutoResumeRunnerCheck outside its own definition"
    for i in call_sites:
        line = supervisor_code_only[max(0, i - 20):i + 40]
        assert "function " not in line, (
            "the only other occurrence of Invoke-AutoResumeRunnerCheck looks like another "
            "definition, not a call:\n%s" % line
        )


# ── runtime checks: the extracted functions against real child processes ──────────────────

_DRIVER_HEADER = '''param(
    [Parameter(Mandatory=$true)][string]$PyExe,
    [Parameter(Mandatory=$true)][string]$OutFile
)

$ErrorActionPreference = "Stop"

# A STUB, NOT THE REAL Write-Log. The real one appends to the machine's actual supervisor log
# file, which a live supervisor is also writing to on this machine right now (see this repo's
# live-system warnings) -- this test must never touch that file. Capturing into a list instead
# is also how the assertions below read the exact line text Invoke-AutoResumeRunnerCheck
# produced.
$script:CapturedLog = New-Object System.Collections.Generic.List[string]
function Write-Log($msg) {
    $script:CapturedLog.Add($msg)
}
'''

# Deliberately a plain (non-f, non-.format) string, concatenated around extracted PowerShell
# text that is full of literal '{'/'}' -- see the sibling test file's identical note.
_DRIVER_FOOTER = r'''
function New-KnownChild([string]$PyExeLocal, [string]$PyCode) {
    # ONE PRE-QUOTED ARGUMENT, NOT SEPARATE -ArgumentList ELEMENTS -- see the sibling test
    # file's comment on the exact failure this avoids (every child reporting ExitCode=1
    # regardless of what it actually did, because PowerShell re-joins an array of ArgumentList
    # elements without quoting the one that contains spaces).
    $argStr = '-c "' + $PyCode + '"'
    return Start-Process -FilePath $PyExeLocal -ArgumentList $argStr -WindowStyle Hidden -PassThru
}

$results = [ordered]@{}
$pRunning = $null
try {
    # STANDS IN FOR "died before argparse": a failed import exits near-instantly with a
    # nonzero code, exactly like a bad path / wrong interpreter / missing dependency would.
    $atQuick = Get-Date
    $pQuick = New-KnownChild $PyExe 'import no_such_module_xyz'
    $pQuick.WaitForExit()
    $cmdQuick = '"' + $PyExe + '" -m relay.fleet_runner --resume'
    Register-AutoResumeRunner -Proc $pQuick -LaunchTime $atQuick -Kind "fleet" -CommandLine $cmdQuick

    # A NORMAL quick clean exit (code 0) -- must NOT be flagged as a quick death even though
    # it is just as fast as the failed import above. Only nonzero + quick is the flagged case.
    $atClean = Get-Date
    $pClean = New-KnownChild $PyExe 'import os; os._exit(0)'
    $pClean.WaitForExit()
    $cmdClean = '"' + $PyExe + '" -m bench.review_run --resume'
    Register-AutoResumeRunner -Proc $pClean -LaunchTime $atClean -Kind "review" -CommandLine $cmdClean

    # Still running at the moment of the check -- must produce NO log line yet.
    $atRunning = Get-Date
    $pRunning = New-KnownChild $PyExe 'import time; time.sleep(30)'
    $cmdRunning = '"' + $PyExe + '" -m relay.fleet_runner --resume'
    Register-AutoResumeRunner -Proc $pRunning -LaunchTime $atRunning -Kind "fleet" -CommandLine $cmdRunning

    # ONE supervisor tick.
    Invoke-AutoResumeRunnerCheck

    $results.LogAfterTick1 = @($script:CapturedLog)
    $results.PendingAfterTick1 = $script:AutoResumeRunners.Count
    $results.QuickCommandLine = $cmdQuick
    $results.CleanCommandLine = $cmdClean
}
finally {
    if ($pRunning) { try { Stop-Process -Id $pRunning.Id -Force -ErrorAction SilentlyContinue } catch { } }
    $results | ConvertTo-Json -Depth 6 | Set-Content -Path $OutFile -Encoding UTF8
}
'''


def _run_driver(fragments: dict, py_exe: str, work_dir: str) -> dict:
    """Write the extracted functions/state plus a small driver to a temp .ps1, run it, return
    the parsed JSON result. All child processes are cleaned up inside the driver's own
    try/finally -- this function never holds a live handle to any of them itself, since
    childproc.run blocks until the whole driver has exited."""
    driver_path = os.path.join(work_dir, "driver.ps1")
    out_path = os.path.join(work_dir, "out.json")
    driver_text = (
        _DRIVER_HEADER + "\n\n"
        + fragments["exit_record_function"] + "\n\n"
        + fragments["runners_list_init"] + "\n\n"
        + fragments["register_function"] + "\n\n"
        + fragments["quick_death_threshold_init"] + "\n\n"
        + fragments["check_function"] + "\n\n"
        + _DRIVER_FOOTER
    )
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
    # -Encoding UTF8 in Windows PowerShell 5.1 writes a BOM; utf-8-sig strips it.
    with open(out_path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def driver_result(tracking_ps_fragments) -> dict:
    work_dir = None
    import tempfile
    work_dir = tempfile.mkdtemp(prefix="runner_dies_before_argparse_")
    try:
        return _run_driver(tracking_ps_fragments, _venv_python(), work_dir)
    finally:
        import shutil as _shutil
        _shutil.rmtree(work_dir, ignore_errors=True)


def test_quick_nonzero_death_is_flagged_with_the_reproduce_command(driver_result):
    lines = driver_result["LogAfterTick1"]
    flagged = [l for l in lines if "did not get far enough to log anything itself" in l]
    assert len(flagged) == 1, (
        "expected exactly one flagged quick-death log line, got:\n%s" % "\n".join(lines)
    )
    line = flagged[0]
    assert "fleet auto-resume runner died after" in line
    assert "code=0x00000001 (1)" in line
    assert "run the same command by hand to see why: " in line
    assert driver_result["QuickCommandLine"] in line, (
        "the flagged line does not contain the exact reproduce command:\n%s" % line
    )


def test_clean_exit_is_logged_briefly_and_not_flagged(driver_result):
    lines = driver_result["LogAfterTick1"]
    clean = [l for l in lines if "review auto-resume runner" in l]
    assert len(clean) == 1, "expected exactly one review-runner log line, got:\n%s" % "\n".join(lines)
    line = clean[0]
    assert "code=0x00000000 (0)" in line
    assert "did not get far enough" not in line, (
        "a clean exit (code 0) must never be flagged as a quick death:\n%s" % line
    )
    # A clean exit is reported briefly -- it must not also print the reproduce command;
    # that is reserved for the case a human actually needs to go investigate.
    assert driver_result["CleanCommandLine"] not in line


def test_still_running_runner_produces_no_log_line_yet(driver_result):
    lines = driver_result["LogAfterTick1"]
    assert not any("fleet auto-resume runner died" in l and "0x00000001" not in l for l in lines)
    # The still-running entry must remain PENDING (not yet reported) after one tick: exactly
    # the quick-death and the clean-exit entries were resolved, the third (still sleeping) was
    # not.
    assert driver_result["PendingAfterTick1"] == 1
    assert len(lines) == 2, (
        "expected exactly 2 log lines after one tick (quick death + clean exit), got:\n%s"
        % "\n".join(lines)
    )
