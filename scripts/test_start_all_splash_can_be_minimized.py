# -*- coding: utf-8 -*-
"""The startup splash can be minimised (owner request, 2026-09-24).

The splash is TopMost. A startup that waits -- another start_all holding the lock, a slow
tunnel, a dependency update -- kept it above every other window for the whole wait, and the
only control on it was the close button. The owner asked for a minimise button so the person
can get it out of the way without cancelling anything.

This builds the REAL form: it extracts Start-Splash from scripts/start_all.ps1 through the
PowerShell parser (not a regex over the text), runs it, and reads the constructed Form's
properties. The form is never shown, so no window reaches the desktop.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START_ALL = os.path.join(REPO, "scripts", "start_all.ps1")

_PROBE = r"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:START_ALL_PATH, [ref]$null, [ref]$null)
$fn = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Start-Splash' }, $true) | Select-Object -First 1
if (-not $fn) { 'NO_FUNCTION'; exit 3 }
Invoke-Expression $fn.Extent.Text
$s = Start-Splash
if (-not $s) { 'NO_FORM'; exit 4 }
$f = $s.Form
'MinimizeBox=' + $f.MinimizeBox
'ControlBox=' + $f.ControlBox
'ShowInTaskbar=' + $f.ShowInTaskbar
'TopMost=' + $f.TopMost
$f.Dispose()
"""


def _powershell():
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe")


@pytest.mark.skipif(os.name != "nt" or not _powershell(), reason="needs Windows PowerShell + WinForms")
def test_the_splash_has_a_minimise_button_and_a_taskbar_entry():
    env = dict(os.environ, START_ALL_PATH=START_ALL)
    r = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", _PROBE], capture_output=True, text=True, timeout=120, env=env)
    out = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    assert r.returncode == 0, (r.stdout, r.stderr)
    # The request itself.
    assert out.get("MinimizeBox") == "True", out
    # A minimised window with no taskbar entry cannot be brought back: minimising would then be
    # the same as losing it.
    assert out.get("ShowInTaskbar") == "True", out
    # The close button stays (an earlier ControlBox=$false trapped people behind it).
    assert out.get("ControlBox") == "True", out
