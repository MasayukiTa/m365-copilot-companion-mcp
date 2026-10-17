# -*- coding: utf-8 -*-
r"""A checkout with no usable .venv is prepared by the project's OWN bootstrap, on the daily path.

WHAT THIS IS ABOUT. start_all.ps1's Invoke-DependencySync used to answer a missing .venv with
"skipped": the supervisor then refused to run at all ("no usable Python", exit 3) and the only
line a person ever saw pointed at setup.bat -- a file the daily path is not allowed to name.
A half-created .venv (a python.exe that exists but will not run) was no better: the venv check
below reported it as "unknown" and nothing repaired it. Now:

  1. MISSING OR UNRUNNABLE -> PREPARED HERE. When bootstrap.py is present but the venv's own
     python.exe is absent or will not run, bootstrap.py is run with a Python found ON PATH
     (Get-SystemPython) -- NOT with the very .venv interpreter that is missing -- through its
     own two steps, ensure_venv and install_deps; the outcome is "installed", which is what the
     supervisor needs to carry on.
  2. A FAILURE IS RECORDED WITH ITS STAGE AND THE REMEDY. A bootstrap step that exits non-zero
     counts one failure carrying bootstrap's own exit code and the remedy
     ("re-run start_all.bat or quickstart.bat") -- in the list, in .setup\last_failure.txt
     (stage=deps_sync) and in the summary file -- and never setup.bat.
  3. A USABLE VENV IS LEFT ALONE. A venv whose python runs goes down the ordinary --check-deps
     path: no ensure_venv, no install_deps, nothing said about preparing anything.
  4. A VENV WHOSE PYTHON WILL NOT RUN IS REBUILT. Existence is not usability: the same repair as
     (1) happens for a python.exe that is there but is not an interpreter.

HOW. Same method as scripts/test_start_all_dependency_drift_check.py and
scripts/test_a_failed_start_says_what_and_where.py: the named functions are cut out of
scripts/start_all.ps1 by balanced-brace extraction and run in a separate powershell against a
temp "checkout". bootstrap.py is a TINY STUB in the temp tree that only records the argv and
the interpreter it was run with and exits with a chosen code -- no pip, no network, no install.
PATH is narrowed to the interpreter running the tests plus SystemRoot, so Get-SystemPython has
exactly one real Python to find and nothing else of this machine (no Store alias, no second
Python) can be reached.

Windows-only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

START_ALL = os.path.join(HERE, "start_all.ps1")
_POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")

pytestmark = pytest.mark.skipif(os.name != "nt" or not _POWERSHELL,
                                reason="start_all.ps1 is Windows PowerShell only")

#: The functions the drivers below need. Extracted by balanced-brace block, exactly as the other
#: start_all tests do -- this file never runs start_all.ps1.
_FUNCS = ["Get-SystemPython", "Invoke-DependencySync", "ConvertFrom-DepsSyncOutput",
          "Get-DepsProblemSummary", "Test-DepsInstallInProgress", "Set-PipNetworkEnvironment",
          "Restore-ProcessEnvironment", "Set-StartupStage", "Get-StartupStage",
          "Get-StartAllFailurePath", "Write-StartupFailureFile", "Remove-StartAllFailureFile",
          "Save-StartupFailure", "Write-StartupSummary"]

#: Stands in for bootstrap.py: it records the argv it was given and the interpreter it ran as,
#: and exits with a chosen code when the --only step is the one the test wants to fail. It never
#: installs anything and never touches the network.
_STUB = '''\
import json, os, sys
with open(os.environ["STUB_BOOTSTRAP_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "exe": sys.executable}) + "\\n")
step = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else ""
if step and step == os.environ.get("STUB_FAIL_STEP"):
    sys.exit(int(os.environ.get("STUB_FAIL_RC", "1")))
sys.exit(0)
'''

PREPARING = "Preparing the Python environment automatically"
REMEDY = "re-run start_all.bat or quickstart.bat"


def _source() -> str:
    with open(START_ALL, encoding="utf-8") as fh:
        return fh.read()


def _extract_braced_block(text: str, start_marker: str) -> str:
    """The balanced-brace block starting at `start_marker` (not string-aware; every brace inside
    a string in the extracted functions is itself balanced)."""
    idx = text.index(start_marker)
    depth = 0
    for i in range(text.index("{", idx), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[idx:i + 1]
    raise AssertionError("unbalanced braces at %r" % start_marker)


def _q(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


@pytest.fixture(scope="module")
def functions() -> str:
    src = _source()
    out = []
    for name in _FUNCS:
        m = re.search(r"(?m)^function %s\b" % re.escape(name), src)
        assert m, "start_all.ps1 no longer defines %s" % name
        out.append(_extract_braced_block(src, m.group(0)))
    return "\n\n".join(out)


@pytest.fixture()
def checkout(tmp_path):
    root = tmp_path / "co"
    (root / ".setup").mkdir(parents=True)
    (root / "scripts").mkdir()
    return root


def _stub(checkout) -> str:
    """The stub bootstrap.py, in the temp tree, so nothing of the repo is written to."""
    p = checkout / "scripts" / "stub_bootstrap.py"
    p.write_text(_STUB, encoding="utf-8")
    return str(p)


def _driver(functions, root, body, venv_py, bootstrap_py):
    pre = "\n".join([
        '$ErrorActionPreference = "Continue"',
        "$root = %s" % _q(root),
        "$scriptDir = %s" % _q(os.path.join(str(root), "scripts")),
        "$script:startupFailures = @()",
        '$script:startupStage = "start"',
        '$script:lastFailure = ""',
        '$script:lastFailureStage = ""',
        "$script:splash = $null",
        "$script:splashMessages = @()",
        "$script:venvPy = %s" % _q(venv_py),
        "$script:bootstrapPy = %s" % _q(bootstrap_py),
        'function Hide-Secrets([string]$text) { return $text }',
        'function Set-SplashStatus($s, [string]$t) { $script:splashMessages += $t }',
    ])
    return pre + "\n\n" + functions + "\n\n" + body + "\n"


def _env(log, fail_step="", fail_rc=""):
    """PATH is exactly the test interpreter's own directory plus SystemRoot: Get-SystemPython
    finds one real Python 3.10+ and nothing else on this machine. The three variables that would
    pin a different interpreter are removed."""
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    env = dict(os.environ,
               PATH=";".join([os.path.dirname(sys.executable),
                              os.path.join(sysroot, "System32"), sysroot]),
               STUB_BOOTSTRAP_LOG=str(log),
               STUB_FAIL_STEP=fail_step,
               STUB_FAIL_RC=fail_rc)
    for k in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(k, None)
    return env


def _ps(tmp_path, text, env, timeout=240):
    p = tmp_path / ("drv_%s.ps1" % uuid.uuid4().hex[:8])
    # Forced, as the sibling start_all tests force it: the raw bytes this powershell writes to a
    # redirected stdout pipe are governed by [Console]::OutputEncoding, not by $OutputEncoding.
    text = '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n' + text
    p.write_text(text, encoding="utf-8-sig")
    return childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                         env=env, timeout=timeout)


def _result(r):
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    raise AssertionError("no RESULT line.\nstdout:\n%s\nstderr:\n%s" % (r.stdout[-3000:], r.stderr[-3000:]))


def _console(run) -> str:
    """The lines the process actually wrote to the console. The RESULT: line is this file's own
    transport and echoes whatever the driver sends, so it is not console output and a phrase
    counted there would be counted twice."""
    return "\n".join(l for l in run.stdout.splitlines() if not l.startswith("RESULT:"))


def _entries(log):
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def _failure_file(checkout):
    return checkout / ".setup" / "last_failure.txt"


def _fields(path):
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _s, v = line.partition("=")
            out[k] = v
    return out


def _body() -> str:
    return r"""
$verdict = Invoke-DependencySync $script:venvPy $script:bootstrapPy
"RESULT:" + (@{ verdict = $verdict; failures = @($script:startupFailures);
                splash = @($script:splashMessages) } | ConvertTo-Json -Compress)
"""


def _assert_prepared(r, run, log, checkout):
    """The shape every "no usable venv" case must have: the two bootstrap steps, in order, run by
    the PATH Python -- not by the .venv path that is missing or unusable -- the outcome word the
    supervisor needs, one line about preparing, and no failure recorded."""
    assert r["verdict"] == "installed", r
    assert r["failures"] == [], r
    assert r["splash"] == [PREPARING + "..."], r
    entries = _entries(log)
    assert [e["argv"] for e in entries] == [["--only", "ensure_venv"], ["--only", "install_deps"]], entries
    for e in entries:
        assert os.path.normcase(e["exe"]) == os.path.normcase(sys.executable), \
            "bootstrap.py was run by %r, not the Python on PATH" % (e["exe"],)
    assert _console(run).count(PREPARING) == 1, run.stdout[-2000:]
    assert "setup.bat" not in _console(run), run.stdout[-2000:]
    assert not _failure_file(checkout).exists(), "a successful preparation recorded a failure"

# ==================================================== 1. a missing venv is prepared

def test_a_missing_venv_is_prepared_by_the_projects_own_bootstrap(tmp_path, checkout, functions):
    """No .venv at all is what a ZIP-extracted checkout looks like. Invoke-DependencySync repairs
    it with bootstrap.py's own ensure_venv + install_deps, run by the Python on PATH -- the .venv
    interpreter the branch is missing cannot be the one that makes it -- and returns "installed"."""
    log = tmp_path / "stub_log.jsonl"
    venv_py = checkout / ".venv" / "Scripts" / "python.exe"      # never created
    bootstrap_py = _stub(checkout)
    run = _ps(tmp_path, _driver(functions, checkout, _body(), venv_py, bootstrap_py),
              _env(log))
    r = _result(run)
    _assert_prepared(r, run, log, checkout)


# ==================================================== 2. a failure carries stage and remedy

def test_a_bootstrap_failure_is_recorded_with_its_stage_and_the_remedy(tmp_path, checkout, functions):
    r"""The remedy must be readable after the fact. A failing install_deps (exit 5) counts exactly
    one failure naming bootstrap's own exit code and "re-run start_all.bat or quickstart.bat" --
    in the list, in .setup\last_failure.txt under stage=deps_sync, and in the summary file -- and
    never setup.bat."""
    log = tmp_path / "stub_log.jsonl"
    summary = checkout / ".setup" / "logs" / "start_all_summary.txt"
    venv_py = checkout / ".venv" / "Scripts" / "python.exe"      # never created
    bootstrap_py = _stub(checkout)
    body = (r"""
$verdict = Invoke-DependencySync $script:venvPy $script:bootstrapPy
$null = Write-StartupSummary %s @($script:startupFailures) 'full'
"RESULT:" + (@{ verdict = $verdict; failures = @($script:startupFailures);
                splash = @($script:splashMessages) } | ConvertTo-Json -Compress)
""" % _q(summary))
    run = _ps(tmp_path, _driver(functions, checkout, body, venv_py, bootstrap_py),
              _env(log, fail_step="install_deps", fail_rc="5"))
    r = _result(run)
    assert r["verdict"] == "failed", r
    assert len(r["failures"]) == 1, r
    line = r["failures"][0]
    assert REMEDY in line, line
    assert "setup.bat" not in line, line
    assert "install_deps exited 5" in line, line
    # ensure_venv ran and succeeded; install_deps is the step that failed.
    assert [e["argv"] for e in _entries(log)] == [["--only", "ensure_venv"], ["--only", "install_deps"]]
    assert _console(run).count(PREPARING) == 1, run.stdout[-2000:]
    fields = _fields(_failure_file(checkout))
    assert fields["stage"] == "deps_sync", fields
    assert fields["failures"] == "1", fields
    assert REMEDY in fields["failure"], fields
    text = summary.read_text(encoding="utf-8")
    assert REMEDY in text, text
    assert "setup.bat" not in text, text


# ==================================================== 3. a usable venv is left alone

def test_a_healthy_venv_is_left_alone(tmp_path, checkout, functions):
    """A venv whose python RUNS must go down the ordinary path: --check-deps only, no ensure_venv,
    no install_deps, nothing said about preparing anything, and no failure recorded. The real
    interpreter running these tests is the "working .venv" here."""
    log = tmp_path / "stub_log.jsonl"
    bootstrap_py = _stub(checkout)
    run = _ps(tmp_path, _driver(functions, checkout, _body(), sys.executable, bootstrap_py),
              _env(log))
    r = _result(run)
    assert r["verdict"] == "ok", r
    assert r["failures"] == [], r
    assert r["splash"] == [], r
    assert [e["argv"] for e in _entries(log)] == [["--check-deps"]], _entries(log)
    assert PREPARING not in _console(run), run.stdout[-2000:]
    assert not _failure_file(checkout).exists(), "a healthy venv recorded a failure"


# ==================================================== 4. an unrunnable venv is rebuilt

def test_a_venv_whose_python_will_not_run_is_rebuilt(tmp_path, checkout, functions):
    """EXISTS IS NOT USABLE. A half-created .venv has a python.exe that is there but is not an
    interpreter; the same two bootstrap steps must repair it rather than the check reporting
    "unknown" over a venv nothing ever repairs. The python.exe here is a text file."""
    log = tmp_path / "stub_log.jsonl"
    venv_py = checkout / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("this is not an interpreter\n", encoding="ascii")
    bootstrap_py = _stub(checkout)
    run = _ps(tmp_path, _driver(functions, checkout, _body(), venv_py, bootstrap_py),
              _env(log))
    r = _result(run)
    _assert_prepared(r, run, log, checkout)
