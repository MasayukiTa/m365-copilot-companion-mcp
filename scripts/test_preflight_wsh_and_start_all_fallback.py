# -*- coding: utf-8 -*-
r"""Windows Script Host detection and start_all.bat's fallback off it (START-16, 2026-09-24).

Two things this review found:
  * preflight_policy.ps1 already detected a disabled WSH at INSTALL time, but only WARNed
    (Code=0) -- so setup.bat continued, and nothing about that finding was ever checked again.
  * start_all.bat (the DAILY launcher, run on every later double-click / logon / desktop icon)
    unconditionally ran `wscript.exe scripts\start_all_hidden.vbs` with no error handling at all.
    When WSH is disabled, wscript.exe does NOTHING -- no window, no error, exit code 0 -- so the
    whole stack silently fails to start and nothing on screen says why.

Fixes exercised here:
  * preflight_policy.ps1 -CheckWshOnly: a small, direct entry point start_all.bat can call on
    every run (not just at install time) that reuses the SAME Test-WshEnabled check setup.bat's
    own preflight already had. PREFLIGHT_TEST_WSH_ENABLED (documented "tests only" in the
    script) forces the answer so this can be tested without touching the real machine's WSH
    registry keys.
  * start_all.bat: asks that check FIRST (so a disabled WSH is caught even though wscript.exe's
    own exit code would say nothing about it), and ALSO falls back if wscript.exe itself reports
    a failure. Either way it starts the stack directly via a detached, hidden PowerShell process
    running scripts\start_all.ps1, instead of doing nothing silently.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

from tools import childproc  # noqa: E402
from _install_path_harness import crlf_copy, minimal_path, CSC  # noqa: E402

START_ALL_BAT = os.path.join(REPO, "start_all.bat")
PREFLIGHT_PS1 = os.path.join(REPO, "scripts", "preflight_policy.ps1")

SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = shutil.which("powershell") or os.path.join(
    SYSROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not os.path.isfile(POWERSHELL),
    reason="Windows-only cmd/PowerShell scripts (os.name=%r)" % os.name)


# ---- -CheckWshOnly: the entry point start_all.bat relies on --------------------------------

def run_check_wsh_only(env_override):
    env = dict(os.environ)
    env.pop("PREFLIGHT_TEST_WSH_ENABLED", None)
    if env_override is not None:
        env["PREFLIGHT_TEST_WSH_ENABLED"] = env_override
    return childproc.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PREFLIGHT_PS1,
         "-CheckWshOnly"],
        env=env, timeout=30)


def test_check_wsh_only_reports_forced_disabled():
    r = run_check_wsh_only("0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WSH-ENABLED=0" in r.stdout, r.stdout


def test_check_wsh_only_reports_forced_enabled():
    r = run_check_wsh_only("1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WSH-ENABLED=1" in r.stdout, r.stdout


def test_check_wsh_only_default_reports_a_well_formed_answer():
    # Not asserting which way (that depends on the real machine this happens to run on) --
    # just that the entry point start_all.bat depends on answers in the documented shape.
    r = run_check_wsh_only(None)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WSH-ENABLED=0" in r.stdout or "WSH-ENABLED=1" in r.stdout, r.stdout


# ---- start_all.bat's fallback ---------------------------------------------------------------

WSCRIPT_STUB_CS = r"""
using System; using System.IO;
class WscriptStub {
    static int Main(string[] a) {
        string log = Environment.GetEnvironmentVariable("WSCRIPT_STUB_LOG");
        if (!String.IsNullOrEmpty(log)) File.AppendAllText(log, String.Join(" ", a) + Environment.NewLine);
        string rc = Environment.GetEnvironmentVariable("WSCRIPT_STUB_RC");
        int code = 0;
        if (!String.IsNullOrEmpty(rc)) { int.TryParse(rc, out code); }
        return code;
    }
}
"""

START_ALL_PS1_STUB = (
    "$repo = Split-Path -Parent $PSScriptRoot\n"
    "Set-Content -LiteralPath (Join-Path $repo '.fallback_marker.txt') -Value 'ran' -Encoding ascii\n"
)

def _build_wscript_stub(tmp_path: Path) -> Path:
    d = tmp_path / "wscriptstub"
    d.mkdir()
    cs = d / "wscriptstub.cs"
    cs.write_text(WSCRIPT_STUB_CS, encoding="ascii")
    exe = d / "wscript.exe"
    r = childproc.run([CSC, "/nologo", "/out:" + str(exe), str(cs)], timeout=120)
    assert r.returncode == 0 and exe.is_file(), "could not build the wscript stub: %s %s" % (r.stdout, r.stderr)
    return exe


def _build_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "repo"
    (tree / "scripts").mkdir(parents=True)
    crlf_copy(Path(START_ALL_BAT), tree / "start_all.bat")
    crlf_copy(Path(PREFLIGHT_PS1), tree / "scripts" / "preflight_policy.ps1")
    (tree / "scripts" / "win").mkdir(parents=True, exist_ok=True)
    crlf_copy(Path(REPO) / "scripts" / "win" / "wsh_vbs_check.ps1",
              tree / "scripts" / "win" / "wsh_vbs_check.ps1")
    (tree / "scripts" / "start_all_hidden.vbs").write_text("' stub, never actually run by wscript stub\n",
                                                            encoding="ascii")
    (tree / "scripts" / "start_all.ps1").write_text(START_ALL_PS1_STUB, encoding="ascii")
    return tree


def _run_start_all(tree: Path, wscript_dir, wsh_enabled, wscript_rc, log_path):
    env = dict(os.environ)
    env["PATH"] = minimal_path(wscript_dir) if wscript_dir else minimal_path()
    if wsh_enabled is not None:
        env["PREFLIGHT_TEST_WSH_ENABLED"] = wsh_enabled
    if wscript_rc is not None:
        env["WSCRIPT_STUB_RC"] = wscript_rc
    env["WSCRIPT_STUB_LOG"] = str(log_path)
    return childproc.run(["cmd", "/c", str(tree / "start_all.bat")],
                         cwd=str(tree), env=env, timeout=60)


def _wait_for(path: Path, seconds=10) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.2)
    return path.exists()


@pytest.mark.skipif(not os.path.isfile(CSC), reason="csc.exe not available to build the wscript stub")
def test_wsh_enabled_and_wscript_succeeds_does_not_fall_back(tmp_path):
    tree = _build_tree(tmp_path)
    stub = _build_wscript_stub(tmp_path)
    log = tmp_path / "wscript.log"
    marker = tree / ".fallback_marker.txt"

    r = _run_start_all(tree, stub.parent, wsh_enabled="1", wscript_rc="0", log_path=log)
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.exists(), "the stub wscript.exe was never invoked"
    assert "Windows Script Host is disabled" not in r.stdout
    time.sleep(1.5)
    assert not marker.exists(), "fallback ran even though wscript.exe reported success"


@pytest.mark.skipif(not os.path.isfile(CSC), reason="csc.exe not available to build the wscript stub")
def test_wsh_enabled_but_wscript_fails_falls_back(tmp_path):
    tree = _build_tree(tmp_path)
    stub = _build_wscript_stub(tmp_path)
    log = tmp_path / "wscript.log"
    marker = tree / ".fallback_marker.txt"

    r = _run_start_all(tree, stub.parent, wsh_enabled="1", wscript_rc="1", log_path=log)
    assert log.exists(), "the stub wscript.exe was never invoked"
    assert "Windows Script Host is disabled" in r.stdout, r.stdout
    assert _wait_for(marker), "the PowerShell fallback never ran scripts\\start_all.ps1"


@pytest.mark.skipif(not os.path.isfile(CSC), reason="csc.exe not available to build the wscript stub")
def test_wsh_disabled_skips_wscript_entirely_and_falls_back(tmp_path):
    tree = _build_tree(tmp_path)
    stub = _build_wscript_stub(tmp_path)
    log = tmp_path / "wscript.log"
    marker = tree / ".fallback_marker.txt"

    r = _run_start_all(tree, stub.parent, wsh_enabled="0", wscript_rc="0", log_path=log)
    assert not log.exists(), "wscript.exe was invoked even though WSH was reported disabled"
    assert "Windows Script Host is disabled" in r.stdout, r.stdout
    assert _wait_for(marker), "the PowerShell fallback never ran scripts\\start_all.ps1"
