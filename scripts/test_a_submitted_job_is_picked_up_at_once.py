# -*- coding: utf-8 -*-
r"""A job written into .fleet/tasks/pending is handed to the router within a second or two.

MEASURED 2026-09-24: a job submitted with fleet_submit sat ~26 s in pending before the
supervisor's queue pass launched it. Three causes, three fixes, each checked here:

  * `import relay.task_router` took 3.2 s because tools.file_ops -> tools.security imported
    fastmcp at module level (and with it mcp, docket, authlib, requests: ~65 MB of resident
    memory in a process the supervisor starts every tick). tools/security.py now imports it
    when a request is asked about. CHECKED IN A CHILD PROCESS: this test's own interpreter may
    already hold fastmcp from another test, so only a fresh one can say what the import pulls.
    This check is not Windows-only.
  * `devtunnel user show` (3.7 s) ran every tick. scripts/supervisor.ps1 now caches a clear
    "logged in" answer for 10 minutes and drops it at once on a hosting failure, a host exit or
    a failed re-host (Test-DevtunnelLoggedInCached / Clear-DevtunnelLoginCache).
  * The tick ended in `Start-Sleep -Seconds $IntervalSeconds`. It now ends in Wait-ForNextTick,
    which probes pending once a second and runs an express reaper+router pass for a job file
    no pass has been shown, rate-limited to one per 3 s, never re-triggering for a file that
    stays pending, and returning by the tick's fixed deadline however many files arrive.

WHY EXTRACT RATHER THAN DOT-SOURCE: the same reason as
scripts/test_a_silent_death_leaves_its_exit_code.py -- supervisor.ps1 has a `while ($true)`
loop and takes the machine-wide `Global\m365-copilot-companion-supervisor` mutex, and one is
running on the development machine. The functions are cut out by balanced braces and run
with stubs in an isolated powershell.exe, against a temp directory. The express pass in the
driver is a stub that records when it was called: nothing here runs the real router, which
reads the real .fleet and would be a second deliverer next to the live supervisor.

The PowerShell half is Windows-only (supervisor.ps1 is).
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

windows_only = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason=("scripts/supervisor.ps1 is Windows/PowerShell-only (os.name=%r, powershell found=%r)"
            % (os.name, bool(_POWERSHELL))),
)

_HEAVY_ROOTS = ("fastmcp", "mcp", "docket", "authlib")


# -- the import half (every platform) ------------------------------------------------------

def test_importing_the_router_does_not_load_fastmcp():
    """The supervisor runs `python relay/task_router.py --once` on every tick and on every
    express pass; before the fix that import alone was 3.2 s and pulled the whole server
    auth stack. A fresh child interpreter, because this one may have fastmcp already."""
    code = ("import sys, json; import relay.task_router; "
            "print(json.dumps(sorted(m for m in sys.modules "
            "if m.split('.')[0] in %r)))" % (_HEAVY_ROOTS,))
    proc = childproc.run([sys.executable, "-W", "ignore", "-c", code], cwd=REPO, timeout=120,
                         creationflags=childproc.headless_creationflags())
    assert proc.returncode == 0, "import failed:\n%s\n%s" % (proc.stdout, proc.stderr)
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])
    assert loaded == [], (
        "`import relay.task_router` loaded %s -- something on its import chain imports the "
        "server stack at module level again (python -X importtime -c \"import "
        "relay.task_router\" names it)" % loaded)


# -- extracting the PowerShell under test -----------------------------------------------------

def _extract_braced_block(text: str, start_marker: str) -> str:
    """The balanced-brace block starting at the first '{' at/after `start_marker`. Not
    PowerShell-aware; the functions extracted here have no unbalanced brace in a string."""
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
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def _supervisor_source() -> str:
    with open(SUPERVISOR_PS1, "r", encoding="utf-8") as fh:
        return fh.read()


_FUNCTIONS = (
    "function Get-PendingJobNames",
    "function Wait-ForNextTick",
    "function Clear-DevtunnelLoginCache",
    "function Test-DevtunnelLoggedInCached",
)

_DRIVER_HEADER = r"""
param(
    [Parameter(Mandatory=$true)][string]$Scenario,
    [Parameter(Mandatory=$true)][string]$Dir,
    [Parameter(Mandatory=$true)][string]$OutFile
)
$ErrorActionPreference = "Stop"
$script:LogLines = New-Object System.Collections.Generic.List[string]
function Write-Log($msg) { $script:LogLines.Add([string]$msg) }
$script:ExpressSeen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$script:LastExpressPassAt = [datetime]::MinValue
$script:DevtunnelLoginCacheSeconds = 600
$script:DevtunnelLoginCachedUntil = [datetime]::MinValue
$script:DevtunnelLoginCacheUseLogged = $false
$script:DevtunnelLoginAnswerWasCached = $false
$script:DevtunnelLoginRecheckReason = "first check"
$script:LiveChecks = 0
$script:LiveAnswer = $true
function Test-DevtunnelLoggedIn { $script:LiveChecks++; return $script:LiveAnswer }
"""

_DRIVER_FOOTER = r"""
function Start-Writer {
    # Writes <prefix>NN.json at the given offsets (seconds after $T0) from ANOTHER runspace in
    # this process, the way fleet_submit does it: a .tmp first, then a rename into place.
    param([string]$Dir, [double[]]$At, [datetime]$T0)
    $ps = [powershell]::Create()
    [void]$ps.AddScript({
        param($Dir, $At, $T0)
        $written = New-Object System.Collections.Generic.List[object]
        $i = 0
        foreach ($a in $At) {
            $ms = ($T0.AddSeconds($a) - (Get-Date)).TotalMilliseconds
            if ($ms -gt 0) { Start-Sleep -Milliseconds ([int]$ms) }
            $name = "job{0:D2}.json" -f $i
            $tmp = Join-Path $Dir ($name + ".tmp")
            [System.IO.File]::WriteAllText($tmp, "{}")
            [System.IO.File]::Move($tmp, (Join-Path $Dir $name))
            $written.Add([PSCustomObject]@{ name = $name; at = ((Get-Date) - $T0).TotalSeconds })
            $i++
        }
        return $written
    }).AddArgument($Dir).AddArgument($At).AddArgument($T0)
    return @{ ps = $ps; handle = $ps.BeginInvoke() }
}

$result = @{}
if ($Scenario -eq "cache") {
    $script:DevtunnelLoginCacheSeconds = 2
    $steps = New-Object System.Collections.Generic.List[object]
    function Step($label) {
        $a = Test-DevtunnelLoggedInCached
        $steps.Add([PSCustomObject]@{ label = $label; answer = [bool]$a; live = $script:LiveChecks;
                                      cached = [bool]$script:DevtunnelLoginAnswerWasCached })
    }
    Step "first"
    Step "again at once"
    Start-Sleep -Milliseconds 2400
    Step "after expiry"
    Step "again after expiry"
    Clear-DevtunnelLoginCache "tunnel host connections = 0"
    Step "after invalidation"
    Step "again after invalidation"
    $script:LiveAnswer = $false
    Clear-DevtunnelLoginCache "the re-host did not establish"
    Step "invalidated, now logged out"
    Step "logged out again"
    $result.steps = $steps
} else {
    $plan = @{
        single   = @{ seconds = 7;  at = @(1.3) }
        burst    = @{ seconds = 12; at = @(1.0, 1.5, 2.0, 2.5, 3.0) }
        deadline = @{ seconds = 5;  at = @(0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0,
                                           5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0) }
    }[$Scenario]
    $script:Passes = New-Object System.Collections.Generic.List[object]
    $T0 = Get-Date
    $w = Start-Writer -Dir $Dir -At $plan.at -T0 $T0
    Wait-ForNextTick -Seconds $plan.seconds -Dir $Dir -ExpressPass {
        # A stub pass that leaves every file where it is -- the "still pending after a pass"
        # case (blocked, awaiting approval, backoff) is the one that must not re-trigger.
        $script:Passes.Add([PSCustomObject]@{
            at    = ((Get-Date) - $T0).TotalSeconds
            names = [string[]](Get-PendingJobNames -Dir $Dir).ToArray()
        })
    }
    $returnedAt = ((Get-Date) - $T0).TotalSeconds
    $written = $w.ps.EndInvoke($w.handle)
    $w.ps.Dispose()
    $result.returned_at = $returnedAt
    $result.deadline = $plan.seconds
    $result.passes = $script:Passes
    $result.written = @($written)
}
$result.log = $script:LogLines
$result | ConvertTo-Json -Depth 6 | Set-Content -Path $OutFile -Encoding UTF8
"""


def _run_scenario(tmp_path, scenario: str) -> dict:
    src = _supervisor_source()
    funcs = "\n\n".join(_extract_braced_block(src, m) for m in _FUNCTIONS)
    pending = tmp_path / "pending"
    pending.mkdir()
    driver = tmp_path / "driver.ps1"
    out = tmp_path / "out.json"
    driver.write_text(_DRIVER_HEADER + "\n\n" + funcs + "\n\n" + _DRIVER_FOOTER, encoding="utf-8")
    proc = childproc.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver),
         "-Scenario", scenario, "-Dir", str(pending), "-OutFile", str(out)],
        timeout=120,
        creationflags=childproc.headless_creationflags(),
    )
    assert proc.returncode == 0, (
        "driver exited %s\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (proc.returncode, proc.stdout, proc.stderr))
    with open(out, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


@pytest.fixture(scope="module")
def single(tmp_path_factory):
    return _run_scenario(tmp_path_factory.mktemp("single"), "single")


@pytest.fixture(scope="module")
def burst(tmp_path_factory):
    return _run_scenario(tmp_path_factory.mktemp("burst"), "burst")


@pytest.fixture(scope="module")
def deadline(tmp_path_factory):
    return _run_scenario(tmp_path_factory.mktemp("deadline"), "deadline")


@pytest.fixture(scope="module")
def cache(tmp_path_factory):
    return _run_scenario(tmp_path_factory.mktemp("cache"), "cache")


# -- the wait loop ---------------------------------------------------------------------------

@windows_only
def test_a_new_job_file_starts_a_pass_within_two_seconds(single):
    passes = _as_list(single["passes"])
    written = _as_list(single["written"])
    assert passes, "no express pass ran for a job file written 1.3 s into a 7 s wait"
    latency = passes[0]["at"] - written[0]["at"]
    print("file -> express pass latency: %.2f s" % latency)
    assert 0 <= latency <= 2.0, "express pass started %.2f s after the file appeared" % latency
    assert written[0]["name"] in _as_list(passes[0]["names"])


@windows_only
def test_a_job_still_pending_after_its_pass_does_not_trigger_again(single):
    """The stub pass leaves the file in pending for the remaining ~5 s of the wait. Without
    the seen-set that is an express pass every 3 s, i.e. the router every 3 s for as long as
    a job waits on approval or backoff."""
    passes = _as_list(single["passes"])
    assert len(passes) == 1, "expected one pass for one file, got %d at %s" % (
        len(passes), [round(p["at"], 2) for p in passes])


@windows_only
def test_a_burst_of_five_files_is_served_with_passes_at_least_three_seconds_apart(burst):
    passes = _as_list(burst["passes"])
    times = [p["at"] for p in passes]
    gaps = [b - a for a, b in zip(times, times[1:])]
    print("burst pass times: %s" % [round(t, 2) for t in times])
    assert len(passes) >= 2, "five files over 2 s should need at least two passes: %s" % times
    assert all(g >= 2.95 for g in gaps), "express passes closer than 3 s: gaps %s" % gaps
    shown = set()
    for p in passes:
        shown.update(_as_list(p["names"]))
    wrote = {w["name"] for w in _as_list(burst["written"])}
    assert wrote <= shown, "files never shown to any pass: %s" % sorted(wrote - shown)


@windows_only
def test_the_wait_returns_by_its_deadline_while_files_keep_arriving(deadline):
    """Files land every 0.5 s for 9 s against a 5 s wait. The health, stale-code and tunnel
    checks of the full tick must not be postponed by a stream of submissions."""
    passes = _as_list(deadline["passes"])
    print("deadline scenario: returned at %.2f s for a %s s wait, %d passes"
          % (deadline["returned_at"], deadline["deadline"], len(passes)))
    assert passes, "files arrived all through the wait and no express pass ran"
    assert deadline["returned_at"] <= deadline["deadline"] + 1.0, (
        "the wait returned at %.2f s for a %s s deadline" % (deadline["returned_at"],
                                                              deadline["deadline"]))


# -- the devtunnel login cache -----------------------------------------------------------------

@windows_only
def test_the_login_answer_is_cached_rechecked_after_expiry_and_after_invalidation(cache):
    steps = {s["label"]: s for s in _as_list(cache["steps"])}
    assert steps["first"]["live"] == 1 and steps["first"]["answer"] is True
    assert steps["again at once"]["live"] == 1 and steps["again at once"]["cached"] is True
    assert steps["after expiry"]["live"] == 2, "no live re-check after the cache expired"
    assert steps["again after expiry"]["live"] == 2
    assert steps["after invalidation"]["live"] == 3, "an invalidation did not force a re-check"
    assert steps["again after invalidation"]["live"] == 3


@windows_only
def test_a_not_logged_in_answer_is_never_cached(cache):
    """Not-logged-in is when a person runs `devtunnel login` by hand; the next tick must see
    the fix. And "cannot tell" folds into the same $false, which must not be remembered."""
    steps = {s["label"]: s for s in _as_list(cache["steps"])}
    assert steps["invalidated, now logged out"]["answer"] is False
    assert steps["invalidated, now logged out"]["live"] == 4
    assert steps["logged out again"]["live"] == 5


@windows_only
def test_the_cache_says_why_it_rechecked_without_a_line_per_tick(cache):
    log = _as_list(cache["log"])
    assert any("re-checked (tunnel host connections = 0): logged in" in l for l in log), log
    assert any("re-checked (the re-host did not establish): not logged in" in l for l in log), log
    # One "using the cached" line per live "logged in" answer at most -- the driver makes
    # eight calls, and a line per call is exactly the per-tick noise this must not become.
    rechecks = sum(bool(re.search(r"re-checked \(.*\): logged in", l)) for l in log)
    assert rechecks == 3, log
    assert sum("using the cached" in l for l in log) <= rechecks, log


# -- the main loop is wired to them ------------------------------------------------------------

@windows_only
def test_the_main_loop_waits_with_the_express_probe_and_reaps_before_it_routes():
    src = _supervisor_source()
    loop = _strip_ps_comment_lines(src[src.index("\nwhile ($true) {"):])
    assert "Start-Sleep -Seconds $IntervalSeconds" not in loop
    wait = _extract_braced_block(loop, "Wait-ForNextTick -Seconds $IntervalSeconds")
    assert re.search(r"Invoke-FleetReap\s+Invoke-QueueDrain", wait), wait
    assert "Test-DevtunnelLoggedInCached" in loop
    assert not re.search(r"Test-DevtunnelLoggedIn\b(?!Cached)", loop), (
        "the main loop asks devtunnel directly again, every tick")
