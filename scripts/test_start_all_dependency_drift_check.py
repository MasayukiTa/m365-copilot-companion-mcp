# -*- coding: utf-8 -*-
r"""start_all.ps1 must notice a requirements.txt that changed since the last setup run (D5 of
the new-PC install review), and must never try to fix it itself.

BACKGROUND. scripts/bootstrap.py --check-deps (added in 74989c3) compares the live
requirements.txt's sha256 against the hash install_deps stamped into .setup\state.json on its
last success, exiting 3 on a mismatch (or when install_deps was never marked done) and 0 when
they match. start_all.ps1 -- the DAILY launcher, run after every `git pull` -- never called it,
so a dependency added by a pull stayed missing until someone happened to run setup.bat again
(itself unreliable: bootstrap.py:1198-1201 only reinstalls once `verify` has failed once,
i.e. the SECOND setup run after the pull). scripts/start_all.ps1's Test-DependenciesAreStale
wraps that check as a small, testable PowerShell function, called from Invoke-Startup right
after the unlock-password repair and counted into $script:startupFailures exactly the same way
that repair already was -- so the exit code (startup failure count) reflects it and doctor's
"run setup.bat" advice has something concrete behind it.

WHY THIS DOES NOT INSTALL ANYTHING ITSELF. See Test-DependenciesAreStale's own header comment
in scripts/start_all.ps1 (extracted verbatim by this test, so it cannot drift from what is
actually reasoned about there): the daily launcher runs hidden (window 0, exit code nobody
reads) and sometimes twice at once (Startup .lnk + scheduled Task, 15 s apart) -- pip is slow,
needs proxy/trusted-host handling, and an unattended install racing itself into one .venv is
the exact corruption class the install-path review's "two quickstarts at once" section (5.3)
already warns about.

HOW THIS RUNS. The extracted function is driven against a STUB "python" -- not a real
interpreter, and NOT bootstrap.py itself -- that does nothing but exit with a controlled code,
so this test is about the PowerShell wiring/decision (Test-Path guards, exit-code mapping,
$LASTEXITCODE read timing), not about bootstrap.py's own hashing (that already has its own
tests in scripts/test_bootstrap.py).
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402  (see: repository ratchet against text=True)

START_ALL_PS1 = os.path.join(REPO, "scripts", "start_all.ps1")

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason="start_all.ps1 and this test are Windows/PowerShell-only "
           "(os.name=%r, powershell found=%r)" % (os.name, bool(_POWERSHELL)),
)


def _extract_braced_block(text: str, start_marker: str) -> str:
    """The balanced-brace block starting at the first '{' at/after `start_marker`. Same
    approach as scripts/test_a_silent_death_leaves_its_exit_code.py; checked by hand that
    Test-DependenciesAreStale has no unbalanced brace inside a string literal."""
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


@pytest.fixture(scope="module")
def start_all_source() -> str:
    with open(START_ALL_PS1, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def dependency_drift_function(start_all_source: str) -> str:
    return _extract_braced_block(start_all_source, "function Test-DependenciesAreStale")


def _strip_ps_comment_lines(text: str) -> str:
    """Drop every line that is, after leading whitespace, a '#' comment. Same helper as
    scripts/test_a_silent_death_leaves_its_exit_code.py, duplicated per this repo's existing
    convention (see that file's own docstring on this helper) -- needed here because this
    function's own header comment explains it in prose using the words 'pip' and
    'setup.bat', which would otherwise false-positive the code-level check below."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def test_the_function_only_reports_never_installs(dependency_drift_function: str):
    """Source-level guard: this function must never itself invoke pip/setup.bat -- it hands
    the decision back to the caller (see Invoke-Startup's own comment) as a boolean."""
    lowered = _strip_ps_comment_lines(dependency_drift_function).lower()
    assert "pip" not in lowered
    assert "setup.bat" not in lowered
    assert "--check-deps" in dependency_drift_function


_DRIVER_HEADER = '''param(
    [Parameter(Mandatory=$true)][string]$VenvPy,
    [Parameter(Mandatory=$true)][string]$BootstrapPy,
    [Parameter(Mandatory=$true)][string]$OutFile
)
$ErrorActionPreference = "Continue"
'''

_DRIVER_FOOTER = r'''
$result = Test-DependenciesAreStale $VenvPy $BootstrapPy
[string]([bool]$result) | Set-Content -Path $OutFile -Encoding ASCII
'''


def _run_driver(function_text: str, venv_py: str, bootstrap_py: str, tmp_path) -> bool:
    driver_path = os.path.join(str(tmp_path), "driver_%s.ps1" % abs(hash((venv_py, bootstrap_py))))
    out_path = os.path.join(str(tmp_path), "out_%s.txt" % abs(hash((venv_py, bootstrap_py))))
    driver_text = _DRIVER_HEADER + "\n\n" + function_text + "\n\n" + _DRIVER_FOOTER
    with open(driver_path, "w", encoding="utf-8") as fh:
        fh.write(driver_text)

    proc = childproc.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver_path,
         "-VenvPy", venv_py, "-BootstrapPy", bootstrap_py, "-OutFile", out_path],
        timeout=60,
        creationflags=childproc.headless_creationflags(),
    )
    assert proc.returncode == 0, (
        "driver powershell exited %s\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (proc.returncode, proc.stdout, proc.stderr)
    )
    with open(out_path, "r", encoding="ascii") as fh:
        return fh.read().strip() == "True"


_STUB_CHECK_DEPS = """
import os, sys
assert sys.argv[1:] == ["--check-deps"], sys.argv
sys.exit(int(os.environ.get("STUB_CHECK_DEPS_RC", "0")))
"""


@pytest.fixture()
def stub_bootstrap(tmp_path) -> str:
    p = os.path.join(str(tmp_path), "stub_bootstrap.py")
    with open(p, "w", encoding="ascii") as fh:
        fh.write(_STUB_CHECK_DEPS)
    return p


def test_exit_code_3_means_stale(dependency_drift_function, stub_bootstrap, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_CHECK_DEPS_RC", "3")
    assert _run_driver(dependency_drift_function, sys.executable, stub_bootstrap, tmp_path) is True


def test_exit_code_0_means_not_stale(dependency_drift_function, stub_bootstrap, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_CHECK_DEPS_RC", "0")
    assert _run_driver(dependency_drift_function, sys.executable, stub_bootstrap, tmp_path) is False


def test_a_missing_venv_python_is_not_stale_it_is_unknown(dependency_drift_function, stub_bootstrap, tmp_path):
    """No .venv yet is covered by other startup checks (the first-time setup gate, the
    supervisor's own refusal) -- this function must not invent a dependency-drift failure on
    top of "nothing is installed at all"."""
    missing_py = os.path.join(str(tmp_path), "no-such-python.exe")
    assert _run_driver(dependency_drift_function, missing_py, stub_bootstrap, tmp_path) is False


def test_a_missing_bootstrap_script_is_not_stale_it_is_unknown(dependency_drift_function, tmp_path):
    missing_bootstrap = os.path.join(str(tmp_path), "no-such-bootstrap.py")
    assert _run_driver(dependency_drift_function, sys.executable, missing_bootstrap, tmp_path) is False


def test_wired_into_invoke_startup_and_counted(start_all_source: str):
    """The function existing is not enough -- Invoke-Startup must actually call it and count a
    stale result as a startup failure, the same way the unlock-password repair is counted just
    above it (see this test file's header)."""
    call_idx = start_all_source.index("Test-DependenciesAreStale $script:venvPy $script:bootstrapPy")
    window = start_all_source[call_idx:call_idx + 400]
    assert "$script:startupFailures +=" in window, (
        "Test-DependenciesAreStale is called but its result is not counted into "
        "$script:startupFailures:\n%s" % window
    )
    assert "run setup.bat" in window
