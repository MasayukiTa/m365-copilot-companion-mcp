# -*- coding: utf-8 -*-
"""configure_env.ps1's dialog actually gets a Japanese-capable font (owner-reported bug,
2026-09-24).

Same pattern as scripts/test_start_all_splash_can_be_minimized.py: extract the
New-ConfigureEnvForm function from the real script through the PowerShell parser (not a regex
over the text), run it with minimal args, and read the constructed (never shown) Form's Font
off the returned hashtable. The form is never shown, so no window reaches the desktop."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIGURE_ENV = os.path.join(REPO, "scripts", "configure_env.ps1")

_PROBE = r"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:CONFIGURE_ENV_PATH, [ref]$null, [ref]$null)
$fn = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'New-ConfigureEnvForm' }, $true) | Select-Object -First 1
if (-not $fn) { 'NO_FUNCTION'; exit 3 }
# New-ConfigureEnvForm calls Get-EnvVal (for each field) and Get-JapaneseUiFont, so both must
# be defined too -- extract Get-EnvVal the same way rather than re-implementing it, and both
# Add-Type assemblies it needs must already be loaded (the real script does this before the
# function definitions; we replicate that here rather than re-parsing the whole file).
$getEnvVal = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-EnvVal' }, $true) | Select-Object -First 1
if (-not $getEnvVal) { 'NO_GETENVVAL'; exit 5 }
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path (Split-Path -Parent $env:CONFIGURE_ENV_PATH) "win/ui_font.ps1")
Invoke-Expression $getEnvVal.Extent.Text
Invoke-Expression $fn.Extent.Text

$fields = @(@{ Key = "MCP_IMPL_AGENT_URL"; Label = "test"; Hint = "test" })
$built = New-ConfigureEnvForm -fields $fields -Reason "" -lines @() -defaults @{}
if (-not $built -or -not $built.Form) { 'NO_FORM'; exit 4 }
$f = $built.Form
'FontFamilyName=' + $f.Font.FontFamily.Name
$f.Dispose()

# Also report which of the three preferred fonts is actually installed on this machine, so the
# test can tell "wrong font" apart from "none of the preferred fonts exist here".
$installed = (New-Object System.Drawing.Text.InstalledFontCollection).Families | ForEach-Object { $_.Name }
foreach ($p in @("Yu Gothic UI", "Meiryo UI", "MS UI Gothic")) {
    if ($installed -contains $p) { 'PreferredInstalled=' + $p; break }
}
"""


def _powershell():
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe")


@pytest.mark.skipif(os.name != "nt" or not _powershell(), reason="needs Windows PowerShell + WinForms")
def test_the_configure_env_form_gets_a_japanese_capable_font():
    env = dict(os.environ, CONFIGURE_ENV_PATH=CONFIGURE_ENV)
    r = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", _PROBE], capture_output=True, text=True, timeout=120, env=env,
                       errors="replace")
    assert r.returncode == 0, (r.stdout, r.stderr)
    out = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    name = out.get("FontFamilyName")
    assert name, (r.stdout, r.stderr)

    preferred = {"Yu Gothic UI", "Meiryo UI", "MS UI Gothic"}
    if name in preferred:
        return  # the fix picked one of the preferred Japanese fonts -- pass.

    preferred_installed = out.get("PreferredInstalled")
    if not preferred_installed:
        pytest.skip("none of Yu Gothic UI / Meiryo UI / MS UI Gothic are installed on this "
                    "machine, so the helper's documented fallback (SystemFonts.DefaultFont) "
                    "is expected -- got %r" % name)

    # A preferred font IS installed but the form did not get it: this is the actual bug
    # symptom (still on the buggy WinForms default).
    assert name != "Microsoft Sans Serif", (
        "configure_env.ps1's dialog is still on the WinForms default font (%r) even though "
        "%r is installed -- the Japanese-glyph bug is back" % (name, preferred_installed)
    )
    assert name in preferred, (name, out)
