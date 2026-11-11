# -*- coding: utf-8 -*-
r"""A fresh supervisor ADOPTS a live orphan reader instead of starting a second one.

THE INCIDENT (measured 2026-11-11/12, owner had to redo the Dev Tunnel sign-in 4 times in one
night). supervisor pid 25644 died at 23:58:02 (0xC00000FD) but its devtunnel host reader pid 1144
(python -m relay.tunnel_host_log, started 22:52) kept running as an ORPHAN -- a live reader whose
parent supervisor is gone. At 00:29:46 start_all.ps1 -CoreOnly started a fresh supervisor; at
00:32:04 it launched reader pid 33632 while pid 1144 was STILL running: two Dev Tunnel host
readers for one tunnel at once, the state commit 6cfabfd9's design rule forbids and the
2026-11-02 double-host outage proved harmful (the two hosts took each other's relay session and
the gateway answered 502/timeouts).

WHAT IS PINNED HERE
  * the PURE decider (scripts/supervisor_host_launch.decide_adopt_reader): we already track a
    reader -> "none"; we track none and >=1 live reader exists -> "adopt"; no reader exists, or
    an unreadable count -> "none". Exercised directly on every platform.
  * the supervisor WIRES it: Invoke-AdoptOrphanReader reaches the decider the cross-language way
    the other gates do (& $Py -c ... supervisor_host_launch.decide_adopt_reader), only ever
    records an already-live process as $script:TunnelHostProc, and NEVER starts, stops, kills or
    signs anything (no Stop-Process, no devtunnel CLI). The startup fast path calls it before the
    "none" branch that would otherwise launch a second reader.
  * the adopt helper adds no literal "while ($true) {" (other tests locate the main loop by that).

HOW THIS RUNS. The pure table needs no PowerShell. The cross-language table drives the extracted
Invoke-AdoptReaderDecision over a temp driver that puts the worktree's scripts/ on sys.path and
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

#: (found_reader_count, our_reader_tracked) -> expected word.
_TABLE = [
    (1, False, "adopt"),           # an orphan exists and we own none -> adopt it
    (2, False, "adopt"),           # more than one (the very duplicate): still adopt, not launch
    (1, True, "none"),             # we already own a reader -> never claim a second
    (0, True, "none"),             # we own one, none else seen
    (0, False, "none"),            # ordinary first boot: nothing to adopt, launch normally
    (None, False, "none"),         # unreadable count while owning none -> never adopt on a blank
    ("not-a-number", False, "none"),  # malformed count -> none
    (5, True, "none"),             # owning one wins even when many are seen
]


@pytest.mark.parametrize("count,tracked,expected", _TABLE)
def test_the_pure_decider(count, tracked, expected):
    assert H.decide_adopt_reader(count, tracked) == expected


def test_owning_a_reader_always_wins():
    # The first gate is "do we already track our own reader": if yes, the count is irrelevant and
    # the answer is always "none" (adopting another would be claiming a second reader).
    for n in (0, 1, 2, 99, None, "x"):
        assert H.decide_adopt_reader(n, True) == "none"


def test_an_unreadable_count_never_adopts():
    # A blank or malformed process-table count must never be the reason a supervisor claims a
    # reader -- err on the side of NOT adopting (the normal launch path then runs).
    for bad in (None, "", "x", "1.5x"):
        assert H.decide_adopt_reader(bad, False) == "none"


# -- every platform: the supervisor wires it --------------------------------------------------

def test_adopt_helper_reaches_the_pure_decider():
    src = _read(SUPERVISOR_PS1)
    assert "supervisor_host_launch.decide_adopt_reader(" in src, \
        "Invoke-AdoptOrphanReader must reach the pure decider the cross-language way (& $Py -c ...), " \
        "so tools/unreached.py credits the caller and the two cannot drift"
    assert "Invoke-AdoptReaderDecision" in src
    assert "function Invoke-AdoptOrphanReader" in src


def test_startup_fast_path_adopts_before_it_could_launch_a_second_reader():
    # The adopt call must appear in the logged-in startup branch, BEFORE the branch that launches
    # a reader. A simple ordering check: Invoke-AdoptOrphanReader is called earlier in the file
    # than the startup "hosting the tunnel now, without the debounce" launch log.
    src = _read(SUPERVISOR_PS1)
    call_idx = src.index("if (Invoke-AdoptOrphanReader)")
    launch_idx = src.index("hosting the tunnel now, without the debounce")
    assert call_idx < launch_idx, \
        "adoption must run before the fast-path Start-TunnelHost, or a second reader can still start"


_DQ_STRING = __import__("re").compile(r'"(?:[^"`]|`.)*"')


def _code_no_strings(text):
    # Comments AND double-quoted string literals removed, so a word that appears only inside a
    # Write-Log message (e.g. the useful log line "adopted orphan devtunnel host reader ...") is
    # not mistaken for a CLI invocation. What remains is the executable shape of the code.
    return _DQ_STRING.sub('""', _code_only(text))


def test_adoption_never_starts_stops_or_signs_anything():
    # Adoption only takes ownership of an already-live process. It must not stop a process, and
    # must not EXECUTE the devtunnel CLI (which could touch the token cache). Checked over the two
    # functions this change introduced, with comments AND string literals stripped so the log line
    # that merely mentions "devtunnel host reader" is not read as a CLI call.
    src = _read(SUPERVISOR_PS1)
    for fn in ("function Invoke-AdoptOrphanReader", "function Invoke-AdoptReaderDecision"):
        code = _code_no_strings(_extract_braced_block(src, fn))
        low = code.lower()
        assert "stop-process" not in low, ("%s must never stop a process" % fn)
        # No devtunnel executable is run: neither the $DevTunnel variable nor a bare `devtunnel`
        # token survives in the string-stripped code of these functions.
        assert "$devtunnel" not in low, ("%s must never run the devtunnel binary" % fn)
        assert "devtunnel" not in low, ("%s must never execute the devtunnel CLI" % fn)
        assert "user login" not in low
        assert "user logout" not in low


def test_no_literal_while_true_in_the_new_adopt_functions():
    src = _read(SUPERVISOR_PS1)
    for fn in ("function Invoke-AdoptOrphanReader", "function Invoke-AdoptReaderDecision"):
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
    decider = _extract_braced_block(src, "function Invoke-AdoptReaderDecision")
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
def test_powershell_adopt_decider_agrees_with_python():
    src = _read(SUPERVISOR_PS1)
    work = tempfile.mkdtemp(prefix="sup_adopt_")
    try:
        body = r'''
$cases = @(
  @{ Count = 1; Tracked = $false },
  @{ Count = 2; Tracked = $false },
  @{ Count = 1; Tracked = $true },
  @{ Count = 0; Tracked = $false },
  @{ Count = 0; Tracked = $true }
)
foreach ($c in $cases) {
  $d = Invoke-AdoptReaderDecision -FoundReaderCount $c.Count -OurReaderTracked $c.Tracked
  Write-Output ("$($c.Count)|$($c.Tracked)|$d")
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
    assert got[("1", "False")] == "adopt"
    assert got[("2", "False")] == "adopt"
    assert got[("1", "True")] == "none"
    assert got[("0", "False")] == "none"
    assert got[("0", "True")] == "none"
    for (count_s, tracked_s), word in got.items():
        tracked = (tracked_s == "True")
        count = int(count_s)
        assert word == H.decide_adopt_reader(count, tracked), (count_s, tracked_s, word)
