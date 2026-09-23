# -*- coding: utf-8 -*-
r"""quickstart.bat's own exit code (SF-15, 2026-09-24 review).

quickstart.bat used to route a doctor failure (or an "unknown" required check) to the same
:after_banner tail as success, and that tail fell off the end of the file with no `exit /b`, so
cmd's own default (0) was reported to anything reading %ERRORLEVEL% -- a caller saw "success"
over a screen of FAIL lines. This runs the REAL tail of quickstart.bat (from the "Sign in to
M365" banner through end-of-file, extracted verbatim by anchor text -- not reimplemented) with a
stub doctor.ps1 standing in for the real health check, and checks the process exit code.

Everything before that point (STEP 1-4, the tunnel/convenience prompts, STEP 5/6) is orchestration
this file already exercises informally by hand; what SF-15 is about is entirely in this tail, so
extracting just the tail keeps the test fast and hermetic (no real server, no real devtunnel, no
real sign-in) while still running quickstart.bat's actual source lines, unmodified.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

QUICKSTART_BAT = os.path.join(REPO, "quickstart.bat")

SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = shutil.which("powershell") or os.path.join(
    SYSROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not os.path.isfile(POWERSHELL),
    reason="quickstart.bat is a Windows cmd script (os.name=%r, powershell=%r)"
           % (os.name, POWERSHELL))

# Anchor text uniquely identifying where the SF-15-relevant tail starts: two blank/banner lines
# before the "Sign in to M365" section header, through end of file (the doctor call, the exit-code
# logic this review adds, and the :after_banner / :release_lock / label tail).
ANCHOR = "echo  Sign in to M365 on the companion browser"


def _extract_tail() -> str:
    text = Path(QUICKSTART_BAT).read_text(encoding="ascii")
    lines = text.splitlines()
    idx = next(i for i, l in enumerate(lines) if ANCHOR in l)
    # Back up over the two banner lines ("echo." and "echo ====...") that precede the anchor.
    start = idx - 2
    assert lines[start].strip() == "echo."
    return "\n".join(lines[start:]) + "\n"


DOCTOR_STUB = """
param()
$rc = 0
if ($env:DOCTOR_STUB_RC) { $rc = [int]$env:DOCTOR_STUB_RC }
$unknown = 0
if ($env:DOCTOR_STUB_UNKNOWN) { $unknown = [int]$env:DOCTOR_STUB_UNKNOWN }
New-Item -ItemType Directory -Force -Path ".setup\\logs" | Out-Null
"unknown=$unknown" | Set-Content -LiteralPath ".setup\\logs\\doctor_summary.txt" -Encoding ascii
if ($rc -gt 0) { Write-Host ("[FAIL] stub doctor reporting " + $rc + " failure(s)") }
else { Write-Host "[OK] stub doctor: all green" }
exit $rc
"""

SIGNIN_STUB = "exit 0\n"


def _build_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "repo"
    (tree / "scripts").mkdir(parents=True)
    driver = "\n".join([
        "@echo off",
        "setlocal EnableExtensions EnableDelayedExpansion",
        "cd /d \"%~dp0\"",
        "set \"QS_EXIT=0\"",
        _extract_tail(),
    ])
    (tree / "quickstart_tail_driver.bat").write_bytes(
        driver.replace("\n", "\r\n").encode("ascii"))
    (tree / "scripts" / "ensure_m365_signin.ps1").write_text(SIGNIN_STUB, encoding="ascii")
    (tree / "scripts" / "doctor.ps1").write_text(DOCTOR_STUB, encoding="ascii")
    shutil.copyfile(Path(REPO) / "scripts" / "quickstart_lock.ps1",
                     tree / "scripts" / "quickstart_lock.ps1")
    return tree


def _run(tree: Path, doctor_rc: int, doctor_unknown: int = 0):
    env = dict(os.environ)
    env["DOCTOR_STUB_RC"] = str(doctor_rc)
    env["DOCTOR_STUB_UNKNOWN"] = str(doctor_unknown)
    return childproc.run(
        ["cmd", "/c", str(tree / "quickstart_tail_driver.bat")],
        cwd=str(tree), env=env, input="", timeout=60)


def test_all_green_exits_zero(tmp_path):
    tree = _build_tree(tmp_path)
    r = _run(tree, doctor_rc=0, doctor_unknown=0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SETUP COMPLETE" in r.stdout, r.stdout


def test_doctor_failures_exit_nonzero_matching_the_failure_count(tmp_path):
    tree = _build_tree(tmp_path)
    r = _run(tree, doctor_rc=3, doctor_unknown=0)
    assert r.returncode == 3, "expected exit 3 (3 FAIL lines), got %r\n%s%s" % (
        r.returncode, r.stdout, r.stderr)
    assert "SETUP INCOMPLETE" in r.stdout, r.stdout


def test_unknown_required_checks_exit_nonzero_not_zero(tmp_path):
    tree = _build_tree(tmp_path)
    r = _run(tree, doctor_rc=0, doctor_unknown=2)
    assert r.returncode != 0, "SETUP NOT CONFIRMED must not report success\n%s%s" % (
        r.stdout, r.stderr)
    assert "SETUP NOT CONFIRMED" in r.stdout, r.stdout
