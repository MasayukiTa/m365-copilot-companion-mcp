# -*- coding: utf-8 -*-
r"""A supervisor started before the .venv existed must move onto it once it does (new-PC D2).

THE DEFECT. scripts/supervisor.ps1 resolved $Py ONCE at startup: the .venv interpreter if it
existed, else `python` from PATH. A supervisor started before setup.bat had created the .venv
(start_all before quickstart, or a logon autostart during setup) kept the PATH python for its
whole life, so after setup completed main.py still crash-looped on ModuleNotFoundError, and
nothing replaced the supervisor.

THE FIX (Update-PythonInterpreter / Test-PythonRuns in supervisor.ps1). Before every launch the
interpreter is re-decided: one Test-Path when the .venv interpreter is already in use; when a
.venv interpreter has appeared that is not in use, it is RUN once and adopted only if it runs,
with a log line; a .venv python.exe that does not run is refused (logged once per reason) and
re-checked no more often than every $script:PyRecheckSeconds.

HOW THIS RUNS. The functions are extracted from the live .ps1 text (balanced braces) into a temp
driver run by `powershell -File`, as scripts/test_a_silent_death_leaves_its_exit_code.py does:
supervisor.ps1 has a forever loop and a machine-wide mutex, and a real supervisor may be running
here. The interpreters are real: a throwaway `python -m venv --without-pip` for "the .venv
appeared", and a text file named python.exe for "a .venv caught half-created". The launch site
itself (Start-Server using the re-decided $Py) is exercised end to end in
scripts/test_supervisor_port_owner.py.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

SUPERVISOR_PS1 = os.path.join(REPO, "scripts", "supervisor.ps1")

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason="scripts/supervisor.ps1 is Windows/PowerShell-only (os.name=%r, powershell found=%r)"
           % (os.name, bool(_POWERSHELL)),
)


def _extract_braced_block(text: str, start_marker: str) -> str:
    """The balanced-brace block starting at the first '{' at/after `start_marker`. Not
    PowerShell-aware; every brace inside a string literal in the extracted functions is itself
    balanced (checked by hand), so naive counting lands on the right closing brace."""
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


def _extract_line(text: str, needle: str) -> str:
    idx = text.index(needle)
    return text[text.rfind("\n", 0, idx) + 1:text.index("\n", idx)]


def _code_only(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


@pytest.fixture(scope="module")
def src() -> str:
    with open(SUPERVISOR_PS1, "r", encoding="utf-8") as fh:
        return fh.read()


def _base_python() -> str:
    return getattr(sys, "_base_executable", None) or sys.executable


# -- source level: every launch site asks first ---------------------------------------------

@pytest.mark.parametrize("func", ["function Start-Server", "function Invoke-FleetAutoResume",
                                  "function Invoke-ReviewAutoResume"])
def test_each_launch_site_re_decides_the_interpreter_before_launching(src, func):
    block = _code_only(_extract_braced_block(src, func))
    assert "Update-PythonInterpreter" in block, "%s launches python without re-deciding which" % func
    assert block.index("Update-PythonInterpreter") < block.index("Start-Process -FilePath $Py"), func


def test_the_tick_re_decides_before_the_reaper_and_the_queue_drain(src):
    loop = _code_only(src[src.index("\nwhile ($true) {"):])
    assert loop.index("Update-PythonInterpreter") < loop.index("Invoke-FleetReap"), \
        "the per-tick python runs (reaper, router) still use the startup interpreter"


# -- runtime: the extracted functions against real interpreters ------------------------------

_DRIVER_HEADER = r'''param(
    [Parameter(Mandatory=$true)][string]$BasePy,
    [Parameter(Mandatory=$true)][string]$MissingVenvPy,
    [Parameter(Mandatory=$true)][string]$BrokenVenvPy,
    [Parameter(Mandatory=$true)][string]$GoodVenvPy,
    [Parameter(Mandatory=$true)][string]$OutFile
)
$ErrorActionPreference = "Stop"
# A STUB Write-Log: the real one appends to the machine's live supervisor log.
$script:CapturedLog = New-Object System.Collections.Generic.List[string]
function Write-Log($msg) { $script:CapturedLog.Add([string]$msg) }
'''

_DRIVER_FOOTER = r'''
# Count the run checks without changing them: the extracted function is renamed and wrapped.
$script:RunChecks = 0
function Test-PythonRuns {
    param([string]$Exe, [int]$TimeoutMs = 20000)
    $script:RunChecks++
    return (Test-PythonRunsReal -Exe $Exe -TimeoutMs $TimeoutMs)
}
function Snap($label) {
    return [ordered]@{ label = $label; py = $script:Py; checks = $script:RunChecks;
                       log = @($script:CapturedLog) }
}
$steps = New-Object System.Collections.Generic.List[object]
try {
    $results = [ordered]@{}
    $results.brokenCheck = Test-PythonRunsReal -Exe $BrokenVenvPy
    $results.goodCheck = Test-PythonRunsReal -Exe $GoodVenvPy

    # The supervisor started on the PATH python, and there is no .venv yet.
    $script:Py = $BasePy
    $script:VenvPy = $MissingVenvPy
    Update-PythonInterpreter "a test launch"
    $steps.Add((Snap "no venv yet"))

    # A .venv python.exe appeared that cannot start (setup.bat mid-way).
    $script:VenvPy = $BrokenVenvPy
    Update-PythonInterpreter "a test launch"
    $steps.Add((Snap "broken venv"))
    Update-PythonInterpreter "a test launch"
    $steps.Add((Snap "broken venv again at once"))
    $script:PyRejectedAt = (Get-Date).AddSeconds(-($script:PyRecheckSeconds + 1))
    Update-PythonInterpreter "a test launch"
    $steps.Add((Snap "broken venv after the recheck interval"))

    # The .venv is complete.
    $script:VenvPy = $GoodVenvPy
    $script:PyRejectedAt = (Get-Date).AddSeconds(-($script:PyRecheckSeconds + 1))
    Update-PythonInterpreter "a test launch"
    $steps.Add((Snap "good venv"))
    Update-PythonInterpreter "a test launch"
    $steps.Add((Snap "good venv again"))

    # It disappears (setup.bat rebuilding it): keep what works, do not flip back.
    $script:VenvPy = $MissingVenvPy
    Update-PythonInterpreter "a test launch"
    $steps.Add((Snap "venv gone"))
    $results.steps = $steps
} finally {
    $results | ConvertTo-Json -Depth 8 | Set-Content -Path $OutFile -Encoding UTF8
}
'''


@pytest.fixture(scope="module")
def driver_result(src):
    work = tempfile.mkdtemp(prefix="sup_py_reresolve_")
    try:
        good_root = os.path.join(work, "good")
        proc = childproc.run([_base_python(), "-m", "venv", "--without-pip",
                              os.path.join(good_root, ".venv")], timeout=180,
                             creationflags=childproc.headless_creationflags())
        assert proc.returncode == 0, "venv creation failed:\n%s\n%s" % (proc.stdout, proc.stderr)
        good_py = os.path.join(good_root, ".venv", "Scripts", "python.exe")
        broken_py = os.path.join(work, "broken", ".venv", "Scripts", "python.exe")
        os.makedirs(os.path.dirname(broken_py))
        with open(broken_py, "w", encoding="ascii") as fh:
            fh.write("not an interpreter\n")
        missing_py = os.path.join(work, "missing", ".venv", "Scripts", "python.exe")

        funcs = [_extract_braced_block(src, "function Test-PythonRuns").replace(
                     "function Test-PythonRuns", "function Test-PythonRunsReal", 1),
                 _extract_line(src, "$script:PyRecheckSeconds ="),
                 _extract_line(src, "$script:PyRejectedAt = $null"),
                 _extract_line(src, "$script:PyRejectedWhy = $null"),
                 _extract_braced_block(src, "function Update-PythonInterpreter")]
        driver = os.path.join(work, "driver.ps1")
        out = os.path.join(work, "out.json")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(_DRIVER_HEADER + "\n\n" + "\n\n".join(funcs) + "\n\n" + _DRIVER_FOOTER)
        proc = childproc.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver,
             "-BasePy", _base_python(), "-MissingVenvPy", missing_py, "-BrokenVenvPy", broken_py,
             "-GoodVenvPy", good_py, "-OutFile", out],
            timeout=180, creationflags=childproc.headless_creationflags())
        assert proc.returncode == 0, "driver failed:\n%s\n%s" % (proc.stdout, proc.stderr)
        with open(out, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        data["paths"] = {"base": _base_python(), "good": good_py, "broken": broken_py}
        return data
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _step(result, label):
    for s in result["steps"]:
        if s["label"] == label:
            s["log"] = s["log"] if isinstance(s["log"], list) else [s["log"]] if s["log"] else []
            return s
    raise AssertionError("no step %r" % label)


def test_the_run_check_tells_an_interpreter_from_a_file_named_python(driver_result):
    assert driver_result["goodCheck"]["Ok"] is True, driver_result["goodCheck"]
    assert driver_result["brokenCheck"]["Ok"] is False, driver_result["brokenCheck"]
    assert driver_result["brokenCheck"]["Why"], "a refusal without a reason"


def test_without_a_venv_the_startup_interpreter_is_kept_silently(driver_result):
    s = _step(driver_result, "no venv yet")
    assert s["py"] == driver_result["paths"]["base"]
    assert s["checks"] == 0 and s["log"] == []


def test_a_venv_that_does_not_run_is_refused_once_and_rechecked_on_a_timer(driver_result):
    base = driver_result["paths"]["base"]
    first = _step(driver_result, "broken venv")
    assert first["py"] == base, "switched to a python.exe that cannot start"
    assert first["checks"] == 1
    assert len(first["log"]) == 1 and "does not run" in first["log"][0], first["log"]
    assert "re-run setup.bat" in first["log"][0], "the refusal does not say what to do"
    again = _step(driver_result, "broken venv again at once")
    assert again["checks"] == 1, "re-ran the check inside the recheck interval (every tick)"
    later = _step(driver_result, "broken venv after the recheck interval")
    assert later["checks"] == 2 and later["py"] == base
    assert len(later["log"]) == 1, "the same refusal was logged twice: %s" % later["log"]


def test_a_venv_that_appears_and_runs_is_adopted_and_the_switch_is_logged(driver_result):
    s = _step(driver_result, "good venv")
    assert s["py"] == driver_result["paths"]["good"], "the supervisor stayed on the PATH python"
    switch = [l for l in s["log"] if "switching" in l]
    assert len(switch) == 1, s["log"]
    assert driver_result["paths"]["base"] in switch[0] and driver_result["paths"]["good"] in switch[0]
    again = _step(driver_result, "good venv again")
    assert again["checks"] == s["checks"], "an interpreter already in use was run-checked again"
    gone = _step(driver_result, "venv gone")
    assert gone["py"] == driver_result["paths"]["good"], "flipped back when the .venv vanished"
    assert sum("switching" in l for l in gone["log"]) == 1, "a second switch was logged"
