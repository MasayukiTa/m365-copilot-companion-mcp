# -*- coding: utf-8 -*-
r"""Missing VBScript engine detection (Windows Sandbox review, 2026-09-24, defect 1).

A Windows Sandbox run of start_all.bat on a PC WITHOUT the VBScript engine showed a blocking
modal ("Windows Script Host: no script engine for ".vbs"") on EVERY click -- 10 clicks meant
10 modals someone had to dismiss by hand. The registry key preflight_policy.ps1's
Test-WshEnabled reads (Windows Script Host\Settings\Enabled) was untouched and reported "WSH
is enabled": that check only ever answered "did an admin turn WSH off", not "is there anything
installed that can actually run a .vbs file". Some Windows 11 builds ship with the VBScript
engine itself removed/deprecated (an optional feature) while leaving that Enabled key alone,
so wscript.exe starts, cannot find an engine for ".vbs", and pops the modal.

Fix exercised here: preflight_policy.ps1's Test-VbsEngineAvailable walks the same registry
chain Windows itself uses to resolve a .vbs double-click (.vbs's ProgID -> that ProgID's
ScriptEngine name -> that engine's CLSID -> that CLSID's InprocServer32 DLL -> the DLL exists
on disk) -- a pure registry/file read that cannot itself show any UI. Test-CanRunVbs is
Test-WshEnabled -and Test-VbsEngineAvailable, and -CheckWshOnly (the single entry point
start_all.bat, make_desktop_shortcut.ps1 and register-supervisor.ps1 all already call) now
reports THAT, so a missing engine is caught the same way a disabled WSH already was -- without
changing any of those three callers, which only ever grepped for the literal "WSH-ENABLED=0"
line -CheckWshOnly prints.

PREFLIGHT_TEST_VBS_ENGINE (tests only, same convention as PREFLIGHT_TEST_WSH_ENABLED) forces
the answer, so this exercises both branches without depending on whether the machine actually
running this test suite happens to have the VBScript engine installed.
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

from tools import childproc  # noqa: E402

PREFLIGHT_PS1 = os.path.join(REPO, "scripts", "preflight_policy.ps1")

SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = shutil.which("powershell") or os.path.join(
    SYSROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not os.path.isfile(POWERSHELL),
    reason="Windows-only PowerShell script (os.name=%r)" % os.name)


def run_check_wsh_only(wsh_override, vbs_override):
    env = dict(os.environ)
    env.pop("PREFLIGHT_TEST_WSH_ENABLED", None)
    env.pop("PREFLIGHT_TEST_VBS_ENGINE", None)
    if wsh_override is not None:
        env["PREFLIGHT_TEST_WSH_ENABLED"] = wsh_override
    if vbs_override is not None:
        env["PREFLIGHT_TEST_VBS_ENGINE"] = vbs_override
    return childproc.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PREFLIGHT_PS1,
         "-CheckWshOnly"],
        env=env, timeout=30)


def test_wsh_enabled_and_engine_present_reports_enabled():
    r = run_check_wsh_only("1", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WSH-ENABLED=1" in r.stdout, r.stdout


def test_wsh_enabled_but_engine_missing_reports_disabled():
    # This is the sandbox's actual failure mode: WSH itself is on, but nothing can run a
    # .vbs. -CheckWshOnly must fold that into the SAME "WSH-ENABLED=0" line every caller
    # already greps for -- not a silent WSH-ENABLED=1 that sends wscript into the modal.
    r = run_check_wsh_only("1", "0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WSH-ENABLED=0" in r.stdout, r.stdout


def test_wsh_disabled_and_engine_present_still_reports_disabled():
    r = run_check_wsh_only("0", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WSH-ENABLED=0" in r.stdout, r.stdout


def test_wsh_disabled_and_engine_missing_reports_disabled():
    r = run_check_wsh_only("0", "0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WSH-ENABLED=0" in r.stdout, r.stdout


def test_default_engine_probe_is_well_formed_and_never_shows_ui():
    # Not asserting which way (depends on the real machine running this) -- just that the
    # registry-only probe returns promptly with a well-formed answer and no hang (which would
    # indicate it fell through to something that can block on UI).
    r = run_check_wsh_only(None, None)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WSH-ENABLED=0" in r.stdout or "WSH-ENABLED=1" in r.stdout, r.stdout
