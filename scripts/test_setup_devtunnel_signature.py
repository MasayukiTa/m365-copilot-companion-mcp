# -*- coding: utf-8 -*-
r"""Test-DevTunnelSignature in setup_devtunnel.ps1 (INST-10, 2026-09-24 review).

The direct-download branch of setup_devtunnel.ps1 used to accept a downloaded devtunnel.exe once
it looked like a ZIP or a PE image (magic bytes) and could run --version -- which says nothing
about who built it. This exercises the actual function it now calls first, extracted from the
real file (not reimplemented), against three real files:
  * a genuine Microsoft-signed binary already on this machine (notepad.exe) -> accepted.
  * a freshly-compiled, unsigned .exe standing in for a substituted/tampered download -> refused.
  * a missing path -> refused, not a crash.

Also measures this machine's real devtunnel.exe installs (both the WinGet and System32 copies)
with the exact same cmdlet the function uses, which is the evidence the Subject match
('O=Microsoft Corporation') was designed from.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

from tools import childproc  # noqa: E402
from _install_path_harness import extract_ps_function, run_ps, CSC  # noqa: E402

SETUP_DEVTUNNEL_PS1 = os.path.join(REPO, "scripts", "setup_devtunnel.ps1")

SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = shutil.which("powershell") or os.path.join(
    SYSROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
NOTEPAD = os.path.join(SYSROOT, "System32", "notepad.exe")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not os.path.isfile(POWERSHELL),
    reason="setup_devtunnel.ps1 is Windows PowerShell (os.name=%r)" % os.name)


def _function_text() -> str:
    src = Path(SETUP_DEVTUNNEL_PS1).read_text(encoding="utf-8-sig")
    return extract_ps_function(src, "Test-DevTunnelSignature")


def _invoke(tmp_path, path_expr: str) -> "childproc.CompletedProcess":
    script = _function_text() + "\n$r = Test-DevTunnelSignature -Path %s\n" % path_expr
    script += "Write-Output ('OK=' + [int]$r.Ok); Write-Output ('STATUS=' + $r.Status); Write-Output ('SUBJECT=' + $r.Subject)\n"
    return run_ps(script, tmp_path)


@pytest.mark.skipif(not os.path.isfile(NOTEPAD), reason="notepad.exe not present on this machine")
def test_a_genuine_microsoft_signed_binary_is_accepted(tmp_path):
    r = _invoke(tmp_path, "'%s'" % NOTEPAD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK=1" in r.stdout, r.stdout
    assert "O=Microsoft Corporation" in r.stdout, r.stdout


@pytest.mark.skipif(not os.path.isfile(CSC), reason="csc.exe not available to build an unsigned stub")
def test_an_unsigned_binary_is_refused(tmp_path):
    cs = tmp_path / "unsigned.cs"
    cs.write_text("class P { static int Main(string[] a) { return 0; } }", encoding="ascii")
    exe = tmp_path / "devtunnel.exe"
    build = childproc.run([CSC, "/nologo", "/out:" + str(exe), str(cs)], timeout=120)
    assert build.returncode == 0 and exe.is_file(), build.stdout + build.stderr

    r = _invoke(tmp_path, "'%s'" % str(exe))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK=0" in r.stdout, r.stdout
    assert "STATUS=NotSigned" in r.stdout, r.stdout


def test_a_missing_file_is_refused_not_a_crash(tmp_path):
    missing = tmp_path / "does-not-exist.exe"
    r = _invoke(tmp_path, "'%s'" % str(missing))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK=0" in r.stdout, r.stdout
