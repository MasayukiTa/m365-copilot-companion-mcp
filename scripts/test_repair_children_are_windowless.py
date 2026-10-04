# -*- coding: utf-8 -*-
"""repair.ps1 must never create a visible PowerShell child from a windowless GUI parent."""
from pathlib import Path

SRC = (Path(__file__).with_name("repair.ps1")).read_text(encoding="utf-8-sig")


def _func(name: str) -> str:
    start = SRC.index(f"function {name}")
    nxt = SRC.find("\nfunction ", start + 10)
    return SRC[start:] if nxt < 0 else SRC[start:nxt]


def test_repair_has_one_explicit_consoleless_powershell_launcher():
    b = _func("Invoke-HiddenPowerShellFile")
    assert "System.Diagnostics.ProcessStartInfo" in b
    assert ".UseShellExecute = $false" in b
    assert ".CreateNoWindow = $true" in b
    assert "ProcessWindowStyle]::Hidden" in b
    assert "RedirectStandardOutput = $true" in b
    assert "RedirectStandardError = $true" in b


def test_doctor_json_does_not_spawn_bare_powershell():
    b = _func("Get-DoctorResults")
    assert "Invoke-HiddenPowerShellFile" in b
    assert "& powershell" not in b.lower()


def test_registry_powershell_repairs_are_structured_not_reparsed_command_strings():
    # Human-readable Cmd remains for dry-run/reporting; execution uses Script + Args.
    for key in ("server_up", "edge_companion", "edge_bridge", "tunnel_owned",
                "tunnel_exists", "tunnel_name_private", "ui_copilotchat", "ui_fleetcockpit"):
        line = next(l for l in SRC.splitlines() if l.strip().startswith(key + " ") or l.strip().startswith(key + "="))
        assert "Script =" in line, (key, line)
    b = _func("Invoke-RepairCommand")
    assert "Invoke-HiddenPowerShellFile" in b
    assert "$entry.Script" in b


def test_no_direct_bare_powershell_file_spawn_remains_in_repair_execution():
    # Display strings are allowed to say "powershell -File". Execution sites are not.
    assert "& powershell -NoProfile -ExecutionPolicy Bypass -File $doctorPs1" not in SRC
    b = _func("Invoke-RepairCommand")
    assert "Invoke-Expression $entry.Cmd" in b  # external tools such as winget only
    assert "Invoke-Expression $cmd" not in b
