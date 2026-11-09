"""No tool call may kill python.exe by image name: that kills the MCP server itself.

Measured 2026-11-09 (.fleet/tool_events.jsonl): fleet workers ran `taskkill /f /im python.exe /t`
and `Get-Process python | Stop-Process -Force` to clean up leftovers, and each one dropped the
gateway for about 3 minutes (the MCP server, the coordinator and every worker are python.exe).
"""
import pytest

from tools.code_exec import kills_every_python


@pytest.mark.parametrize("cmd", [
    "taskkill /f /im python.exe /t 2>&1 | more +0 & echo DONE",
    "taskkill /IM python.exe /F",
    "taskkill /F /IM python.exe /T >nul 2>&1 & echo KILLDONE",
    "taskkill /F /IM py.exe /T",
    'taskkill /F /FI "IMAGENAME eq python.exe"',
    "taskkill /im pythonw.exe /f",
    'powershell -NoProfile -Command "Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force" & echo PSKD',
    "Stop-Process -Name python -Force",
    "Get-Process python | Stop-Process",
    "wmic process where \"name='python.exe'\" delete",
])
def test_image_wide_python_kill_is_refused(cmd):
    assert kills_every_python(cmd) is not None


@pytest.mark.parametrize("cmd", [
    "taskkill /PID 35476 /F 2>nul & python -m pytest tests -q",
    "taskkill /PID 9580 /T /F 2>&1 & echo ===DONE===",
    "taskkill /IM notepad.exe /F",
    "Get-Process python | Where-Object { $_.Id -eq $env:MY_PID } | Stop-Process",
    "Stop-Process -Id 1234 -Force",
    "python -m pytest tools/test_x.py -q",
    "git status",
    "",
])
def test_pid_scoped_and_ordinary_commands_pass(cmd):
    assert kills_every_python(cmd) is None
