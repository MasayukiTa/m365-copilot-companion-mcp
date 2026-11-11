# -*- coding: utf-8 -*-
r"""After devtunnel login returns, the tunnel is re-hosted at once -- not after the debounce.

THE INCIDENT (measured 2026-11-11, the GOAL's window). The Dev Tunnel was unhosted for
63 min while the devtunnel CLI login had expired. The owner signed in at 17:35:44, but the
supervisor only re-hosted at 17:42:26 -- ~7 min later. The gap is the debounce: once login
returns, Invoke-TunnelHostingCheck sees state 'none' and waits FailuresBeforeAction (4)
consecutive 'tunnel host connections = 0' checks (~80 s) before Start-TunnelHost.

WHAT IS PINNED HERE
  * the PURE decider (scripts/supervisor_login_resume.decide_login_resume_rehost):
    rehost_now ONLY when login just flipped down->up AND state is 'none'; every other tick,
    or any state other than 'none', is debounce. A non-string/odd state reads as debounce.
    Exercised directly on every platform.
  * the supervisor WIRES it: Invoke-TunnelHostingCheck takes -LoginJustResumed, reaches the
    decider the cross-language way the other gates do
    (& $Py -c ... supervisor_login_resume.decide_login_resume_rehost), and the main loop's
    login-resume branch passes the one-tick down->up transition into it. The 'none' branch
    only shortcuts the debounce; the live uncached 'logged in' re-check before
    Start-TunnelHost is left in place, so no re-host happens on a cached login answer.
  * no literal "while ($true) {" is added (other tests locate the main loop by that text).
  * on Windows, the extracted PowerShell wrapper agrees with the Python decider across a
    table of (login_just_resumed, state) inputs.

HOW THIS RUNS. The pure table needs no PowerShell. The cross-language table drives the
extracted Invoke-LoginResumeRehostDecision over a temp driver that puts the worktree's
scripts/ on sys.path and uses the REAL supervisor_login_resume.py (so the two cannot
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

import supervisor_login_resume as L  # noqa: E402

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

#: (login_just_resumed, tunnel_state) -> expected word.
_TABLE = [
    (True, "none", "rehost_now"),     # the one case the fast path exists for
    (True, "None", "rehost_now"),     # case-insensitive
    (True, " none ", "rehost_now"),   # whitespace-tolerant
    (True, "ours", "debounce"),       # already ours: nothing to re-host
    (True, "stale", "debounce"),      # stale keeps its own bounded re-host
    (True, "foreign", "debounce"),    # another PC serves it: never fought
    (True, "shared", "debounce"),     # likewise
    (True, "", "debounce"),           # unknown state -> never skip the debounce
    (False, "none", "debounce"),      # steady-state logged-in tick: ordinary debounce
    (False, "ours", "debounce"),
]


@pytest.mark.parametrize("resumed,state,expected", _TABLE)
def test_the_pure_decider(resumed, state, expected):
    assert L.decide_login_resume_rehost(resumed, state) == expected


def test_only_a_true_resume_flag_counts():
    # Truthy-but-not-True values must NOT trip the fast path: the caller passes a real bool,
    # and an accidental 1 / "yes" from a future caller must fall back to the safe debounce.
    assert L.decide_login_resume_rehost(1, "none") == "debounce"
    assert L.decide_login_resume_rehost("yes", "none") == "debounce"
    assert L.decide_login_resume_rehost(None, "none") == "debounce"


def test_a_non_string_state_is_debounce():
    # An unreadable state must never be the reason the debounce is skipped.
    assert L.decide_login_resume_rehost(True, None) == "debounce"
    assert L.decide_login_resume_rehost(True, 123) == "debounce"


# -- every platform: the supervisor wires it --------------------------------------------------

def test_hosting_check_reaches_the_login_resume_decider():
    src = _read(SUPERVISOR_PS1)
    assert "supervisor_login_resume.decide_login_resume_rehost(" in src, \
        "Invoke-TunnelHostingCheck must reach the pure decider the cross-language way " \
        "(& $Py -c ...), so tools/unreached.py credits the caller and the two cannot drift"
    assert "Invoke-LoginResumeRehostDecision" in src


def test_hosting_check_takes_the_login_just_resumed_flag():
    src = _read(SUPERVISOR_PS1)
    block = _extract_braced_block(src, "function Invoke-TunnelHostingCheck")
    assert "$LoginJustResumed" in block, \
        "Invoke-TunnelHostingCheck must take -LoginJustResumed so the login-resume tick can be told apart"
    # The 'none' branch is the only one that may use it to skip the debounce.
    assert "Invoke-LoginResumeRehostDecision" in block


def test_the_main_loop_passes_the_one_tick_transition():
    # The login-resume branch must compute the down->up transition and pass it in -- not pass a
    # constant $true (which would make every logged-in tick skip the debounce).
    src = _read(SUPERVISOR_PS1)
    assert "Invoke-TunnelHostingCheck -LoginJustResumed" in src
    assert '$loginJustResumed = ($loggedIn -eq $false)' in src, \
        "the flag must be the real down->up transition, measured before $loggedIn is set to $true"


def test_the_live_login_recheck_before_rehost_is_kept():
    # Shortening the WAIT must not remove the uncached 'logged in' re-check that gates the
    # actual Start-TunnelHost -- otherwise the fast path could re-host on a cached answer.
    src = _read(SUPERVISOR_PS1)
    block = _extract_braced_block(src, "function Invoke-TunnelHostingCheck")
    assert "DevtunnelLoginAnswerWasCached" in block and "Test-DevtunnelLoggedInCached" in block, \
        "the none-branch live login re-check before Start-TunnelHost must stay in place"


def test_no_literal_while_true_in_the_new_function():
    src = _read(SUPERVISOR_PS1)
    block = _code_only(_extract_braced_block(src, "function Invoke-LoginResumeRehostDecision"))
    assert "while ($true) {" not in block, \
        "Invoke-LoginResumeRehostDecision must use for (;;), not while ($true)"


# -- Windows: the PowerShell wrapper agrees with the Python decider ---------------------------

_DRIVER_HEAD = r'''param([string]$RepoScripts)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $RepoScripts
$Py = "python"
function Write-Log($msg) { }
'''


def _build_driver(work, src):
    wrapper = _extract_braced_block(src, "function Invoke-LoginResumeRehostDecision")
    body = r'''
$cases = @(
  @{ Resumed = $true;  State = "none" },
  @{ Resumed = $true;  State = "ours" },
  @{ Resumed = $true;  State = "stale" },
  @{ Resumed = $true;  State = "foreign" },
  @{ Resumed = $true;  State = "shared" },
  @{ Resumed = $true;  State = "" },
  @{ Resumed = $false; State = "none" },
  @{ Resumed = $false; State = "ours" }
)
foreach ($c in $cases) {
  $d = Invoke-LoginResumeRehostDecision -LoginJustResumed $c.Resumed -TunnelState $c.State
  $s = if ($c.State -eq "") { "EMPTY" } else { $c.State }
  Write-Output ("$($c.Resumed)|$s|$d")
}
'''
    driver = os.path.join(work, "driver.ps1")
    with open(driver, "w", encoding="utf-8") as fh:
        fh.write(_DRIVER_HEAD + "\n" + wrapper + "\n" + body)
    return driver


def _run(work, src):
    from tools import childproc
    scripts_copy = os.path.join(work, "scripts")
    os.makedirs(scripts_copy, exist_ok=True)
    shutil.copy(os.path.join(REPO, "scripts", "supervisor_login_resume.py"), scripts_copy)
    driver = _build_driver(work, src)
    proc = childproc.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver,
         "-RepoScripts", scripts_copy],
        timeout=120, creationflags=childproc.headless_creationflags())
    return proc.stdout.strip().splitlines()


@windows_only
def test_powershell_wrapper_agrees_with_python():
    src = _read(SUPERVISOR_PS1)
    work = tempfile.mkdtemp(prefix="sup_loginresume_")
    try:
        lines = _run(work, src)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    got = {}
    for ln in lines:
        parts = ln.split("|")
        if len(parts) == 3:
            got[(parts[0], parts[1])] = parts[2]
    assert got[("True", "none")] == "rehost_now"
    assert got[("True", "ours")] == "debounce"
    assert got[("True", "stale")] == "debounce"
    assert got[("True", "foreign")] == "debounce"
    assert got[("True", "shared")] == "debounce"
    assert got[("True", "EMPTY")] == "debounce"
    assert got[("False", "none")] == "debounce"
    assert got[("False", "ours")] == "debounce"
    for (resumed_s, state_s), word in got.items():
        resumed = (resumed_s == "True")
        state = "" if state_s == "EMPTY" else state_s
        assert word == L.decide_login_resume_rehost(resumed, state), (resumed_s, state_s, word)
