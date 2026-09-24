# -*- coding: utf-8 -*-
r"""Desktop / Startup shortcuts must not go silently dead when WSH is disabled (D18 / START-16,
2026-09-24, the other half of test_preflight_wsh_and_start_all_fallback.py's finding).

start_all.bat already learned to stop trusting wscript.exe's own exit code and to ask
preflight_policy.ps1 -CheckWshOnly first. But TWO OTHER LAUNCHERS still hard-coded
wscript.exe + a .vbs target, unconditionally, with no WSH check at all:

  * scripts/make_desktop_shortcut.ps1  -- the Desktop icon quickstart.bat offers to create.
  * scripts/register-supervisor.ps1    -- the per-user Startup-folder logon shortcut, AND the
    opportunistic Task Scheduler action it also registers.

On a PC where Windows Script Host is disabled by policy, a shortcut (or scheduled task action)
built this way "runs" at every double-click / every logon and produces nothing at all: no
window, no error, no process, no log line pointing at the cause.

Fixes exercised here: both scripts now ask preflight_policy.ps1 -CheckWshOnly (reusing the same
Test-WshEnabled check, and the same PREFLIGHT_TEST_WSH_ENABLED override the tests use) before
deciding the shortcut/task target:
  * WSH enabled  -> unchanged: wscript.exe running the windowless .vbs.
  * WSH disabled -> powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden
    -File <start_all.ps1> [-NoUi -NoSplash], i.e. exactly the arguments the corresponding .vbs
    itself passes to start_all.ps1, just without wscript.exe in front of them.

Also exercised: BOTH scripts unconditionally OVERWRITE the shortcut/task action on every run
(quickstart.bat calls them unconditionally whenever the person answers yes), so a shortcut that
was created while WSH worked, then went dead because WSH was disabled afterwards, is repaired
simply by re-running the script -- this is asserted directly (build one target, then rebuild
the same .lnk file under the other WSH state and check it changed).

Register-ScheduledTask (and its New-ScheduledTask* helpers) are STUBBED for every run of
register-supervisor.ps1 in this file: letting the real cmdlet through would register (or
unregister) an actual logon task named M365CompanionAutostart on whatever machine runs this
test, which is exactly the "do not run register-supervisor.ps1 for real" rule this suite exists
to honor. The stub is a small PowerShell prelude that defines `function global:<CmdletName>`
for each Task-Scheduler cmdlet the script calls, then `&`-invokes the real, unmodified
register-supervisor.ps1 in the SAME process -- PowerShell's function-before-cmdlet command
resolution means the real cmdlets are never reached. The stubbed Register-ScheduledTask logs
the Action it was given (Execute/Argument/WorkingDirectory) to a file this test then reads,
which is the "task action XML" check in practice (an object, not literal XML, but the same
information Register-ScheduledTask would otherwise turn into the real task's <Actions> XML).

Shortcut (.lnk) files are read back by parsing raw bytes here (UTF-16LE, permissive), not via
the WScript.Shell COM object: reading a shortcut through COM is unaffected by Windows Script
Host's own enabled/disabled setting in practice (that setting gates wscript.exe/cscript.exe
running .vbs/.js files, not COM automation from another host), but this suite's whole point is
not to depend on WSH-adjacent machinery to verify WSH-independent behavior, so it never touches
WScript.Shell at all -- on the read side or, for these tests' own verification, anywhere else.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

REPO_SCRIPTS = Path(REPO) / "scripts"
SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = shutil.which("powershell") or os.path.join(
    SYSROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not os.path.isfile(POWERSHELL),
    reason="Windows-only PowerShell scripts (os.name=%r)" % os.name)


# ---- shared tree / helpers -------------------------------------------------------------------

def _build_tree(tmp_path: Path) -> Path:
    """A throwaway copy of just the files these two scripts need. Never the real repo tree:
    the Desktop/Startup .lnk paths and .setup\\convenience_provisioned all land under tmp_path
    via the env-var overrides convenience_marker.ps1 already supports for exactly this reason.
    """
    tree = tmp_path / "repo"
    (tree / "scripts" / "win").mkdir(parents=True)
    for name in ("make_desktop_shortcut.ps1", "register-supervisor.ps1", "preflight_policy.ps1"):
        shutil.copyfile(REPO_SCRIPTS / name, tree / "scripts" / name)
    shutil.copyfile(REPO_SCRIPTS / "win" / "convenience_marker.ps1",
                     tree / "scripts" / "win" / "convenience_marker.ps1")
    shutil.copyfile(REPO_SCRIPTS / "win" / "wsh_vbs_check.ps1",
                     tree / "scripts" / "win" / "wsh_vbs_check.ps1")
    # Content is irrelevant: these are only ever linked TO in these tests, never executed
    # (wscript.exe itself is never invoked, and start_all.ps1 is never run).
    (tree / "scripts" / "start_all_hidden.vbs").write_text(
        "' stub -- never actually run by these tests\n", encoding="ascii")
    (tree / "scripts" / "start_background_hidden.vbs").write_text(
        "' stub -- never actually run by these tests\n", encoding="ascii")
    (tree / "scripts" / "start_all.ps1").write_text(
        "# stub -- never actually run by these tests, only linked to\n", encoding="ascii")
    return tree


def _lnk_text(path: Path) -> str:
    """Shortcut (.lnk) target/argument strings, read by parsing bytes directly (see module
    docstring for why this avoids WScript.Shell). Good enough to prove which executable and
    which arguments got written, without a full LNK-format parser.

    The .lnk format mixes narrow-ASCII fields (LinkInfo's LocalBasePath, where TargetPath ends
    up) with length-prefixed UTF-16LE fields (StringData, where Arguments ends up) whose start
    offset's PARITY is not fixed -- it depends on the byte length of whatever StringData section
    came before it. Decoding the whole file as UTF-16LE from a single fixed offset silently
    garbles any string that happens to start at the other parity (each 2-byte code unit then
    straddles two unrelated fields). Decoding from BOTH offset 0 and offset 1, alongside a plain
    latin-1 pass for the ASCII fields, covers every alignment a given field can land on.
    """
    assert path.is_file(), "shortcut was not created: %s" % path
    data = path.read_bytes()
    parts = [
        data.decode("utf-16-le", errors="ignore"),
        data[1:].decode("utf-16-le", errors="ignore"),
        data.decode("latin-1", errors="ignore"),
    ]
    return "\n".join(parts)


def _env(wsh_enabled, **extra) -> dict:
    env = dict(os.environ)
    env.pop("PREFLIGHT_TEST_WSH_ENABLED", None)
    if wsh_enabled is not None:
        env["PREFLIGHT_TEST_WSH_ENABLED"] = wsh_enabled
    env.update({k: str(v) for k, v in extra.items()})
    return env


# ---- make_desktop_shortcut.ps1 -----------------------------------------------------------

def _run_make_shortcut(tree: Path, wsh_enabled, desktop_dir: Path):
    env = _env(wsh_enabled, M365_COMPANION_DESKTOP_DIR=desktop_dir)
    script = tree / "scripts" / "make_desktop_shortcut.ps1"
    return childproc.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=str(tree), env=env, timeout=60)


def test_desktop_shortcut_uses_wscript_when_wsh_enabled(tmp_path):
    tree = _build_tree(tmp_path)
    desktop = tmp_path / "Desktop"
    desktop.mkdir()

    r = _run_make_shortcut(tree, "1", desktop)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Windows Script Host is disabled" not in r.stdout

    lnk = desktop / "M365 Companion.lnk"
    text = _lnk_text(lnk)
    assert "wscript.exe" in text.lower(), text
    assert "start_all_hidden.vbs" in text, text
    assert "start_all.ps1" not in text, "should link via the .vbs, not straight to start_all.ps1"


def test_desktop_shortcut_uses_powershell_when_wsh_disabled(tmp_path):
    tree = _build_tree(tmp_path)
    desktop = tmp_path / "Desktop"
    desktop.mkdir()

    r = _run_make_shortcut(tree, "0", desktop)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Windows Script Host is disabled" in r.stdout, r.stdout

    lnk = desktop / "M365 Companion.lnk"
    text = _lnk_text(lnk)
    assert "powershell.exe" in text.lower(), text
    assert "start_all.ps1" in text, text
    assert "-windowstyle" in text.lower() and "hidden" in text.lower(), text
    assert "wscript" not in text.lower(), "should not reference wscript.exe at all: %s" % text
    assert "-noui" not in text.lower(), "Desktop launcher mirrors start_all_hidden.vbs, which " \
        "passes no -NoUi/-NoSplash: %s" % text


def test_desktop_shortcut_self_heals_when_wsh_is_disabled_later(tmp_path):
    """A shortcut built while WSH worked must be REPLACED (not left dead) by simply re-running
    this script after WSH gets disabled -- the whole point of always overwriting."""
    tree = _build_tree(tmp_path)
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    lnk = desktop / "M365 Companion.lnk"

    r1 = _run_make_shortcut(tree, "1", desktop)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    before = _lnk_text(lnk)
    assert "wscript.exe" in before.lower()

    r2 = _run_make_shortcut(tree, "0", desktop)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    after = _lnk_text(lnk)
    assert "powershell.exe" in after.lower(), \
        "re-running after WSH was disabled should have repointed the shortcut: %s" % after
    assert "wscript" not in after.lower()


# ---- register-supervisor.ps1 (Startup shortcut + stubbed Task Scheduler action) --------------

# Stubs every Task-Scheduler cmdlet register-supervisor.ps1 calls, THEN `&`-invokes the real,
# unmodified script in the same process. PowerShell resolves a same-named function ahead of a
# cmdlet, so the real ScheduledTasks module is never reached -- nothing is registered or
# unregistered on the machine actually running this test. Register-ScheduledTask logs the
# Action it received (Execute/Argument/WorkingDirectory) to %TASK_ACTION_LOG%.
_SCHTASK_STUB_PRELUDE = r"""
function global:Get-ScheduledTask {
    [CmdletBinding()]
    param($TaskName)
    return $null
}
function global:Unregister-ScheduledTask {
    [CmdletBinding()]
    param($TaskName, [switch]$Confirm)
}
function global:New-ScheduledTaskAction {
    [CmdletBinding()]
    param($Execute, $Argument, $WorkingDirectory)
    return [pscustomobject]@{ Execute = $Execute; Argument = $Argument; WorkingDirectory = $WorkingDirectory }
}
function global:New-ScheduledTaskTrigger {
    [CmdletBinding()]
    param([switch]$AtLogOn)
    return @{ AtLogOn = [bool]$AtLogOn }
}
function global:New-ScheduledTaskPrincipal {
    [CmdletBinding()]
    param($UserId, $LogonType, $RunLevel)
    return @{ UserId = $UserId }
}
function global:New-ScheduledTaskSettingsSet {
    [CmdletBinding()]
    param(
        [switch]$AllowStartIfOnBatteries,
        [switch]$DontStopIfGoingOnBatteries,
        [switch]$StartWhenAvailable,
        $MultipleInstances,
        $ExecutionTimeLimit
    )
    return @{}
}
function global:Register-ScheduledTask {
    [CmdletBinding()]
    param($TaskName, $Action, $Trigger, $Principal, $Settings)
    $log = $env:TASK_ACTION_LOG
    if ($log) {
        $out = "TaskName=" + $TaskName + [Environment]::NewLine +
               "Execute=" + $Action.Execute + [Environment]::NewLine +
               "Argument=" + $Action.Argument + [Environment]::NewLine +
               "WorkingDirectory=" + $Action.WorkingDirectory
        [System.IO.File]::WriteAllText($log, $out)
    }
    return $null
}

& (Join-Path $PSScriptRoot "register-supervisor.ps1")
"""


def _run_register_supervisor(tree: Path, wsh_enabled, startup_dir: Path, task_log: Path):
    wrapper = tree / "scripts" / "_test_stub_register.ps1"
    wrapper.write_text(_SCHTASK_STUB_PRELUDE, encoding="ascii")
    env = _env(wsh_enabled, M365_COMPANION_STARTUP_DIR=startup_dir, TASK_ACTION_LOG=task_log)
    return childproc.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=str(tree), env=env, timeout=60)


def test_register_supervisor_uses_wscript_when_wsh_enabled(tmp_path):
    tree = _build_tree(tmp_path)
    startup = tmp_path / "Startup"
    startup.mkdir()
    task_log = tmp_path / "task_action.txt"

    r = _run_register_supervisor(tree, "1", startup, task_log)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Windows Script Host is disabled" not in r.stdout

    lnk_text = _lnk_text(startup / "M365 Companion.lnk")
    assert "wscript.exe" in lnk_text.lower(), lnk_text
    assert "start_background_hidden.vbs" in lnk_text, lnk_text

    assert task_log.is_file(), "the stubbed Register-ScheduledTask was never called"
    action = task_log.read_text(encoding="utf-8")
    assert "Execute=" in action and action.split("Execute=", 1)[1].split("\n", 1)[0] \
        .strip().lower().endswith("wscript.exe"), action
    assert "start_background_hidden.vbs" in action, action


def test_register_supervisor_uses_powershell_when_wsh_disabled(tmp_path):
    tree = _build_tree(tmp_path)
    startup = tmp_path / "Startup"
    startup.mkdir()
    task_log = tmp_path / "task_action.txt"

    r = _run_register_supervisor(tree, "0", startup, task_log)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Windows Script Host is disabled" in r.stdout, r.stdout

    lnk_text = _lnk_text(startup / "M365 Companion.lnk")
    assert "powershell.exe" in lnk_text.lower(), lnk_text
    assert "start_all.ps1" in lnk_text, lnk_text
    assert "-noui" in lnk_text.lower(), \
        "logon autostart mirrors start_background_hidden.vbs, which passes -NoUi -NoSplash: %s" % lnk_text
    assert "-nosplash" in lnk_text.lower(), lnk_text
    assert "-windowstyle" in lnk_text.lower() and "hidden" in lnk_text.lower(), lnk_text
    assert "wscript" not in lnk_text.lower(), lnk_text

    assert task_log.is_file(), "the stubbed Register-ScheduledTask was never called"
    action = task_log.read_text(encoding="utf-8")
    assert "Execute=" in action and action.split("Execute=", 1)[1].split("\n", 1)[0] \
        .strip().lower().endswith("powershell.exe"), action
    assert "-NoUi" in action and "-NoSplash" in action, action
    assert "-WindowStyle" in action and "Hidden" in action, action


def test_register_supervisor_self_heals_when_wsh_is_disabled_later(tmp_path):
    """Same self-healing property as the Desktop shortcut: a Startup shortcut (and scheduled
    task action) built while WSH worked must be REPLACED, not left dead, the next time this
    script runs after WSH is disabled."""
    tree = _build_tree(tmp_path)
    startup = tmp_path / "Startup"
    startup.mkdir()
    lnk = startup / "M365 Companion.lnk"
    task_log = tmp_path / "task_action.txt"

    r1 = _run_register_supervisor(tree, "1", startup, task_log)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    before_lnk = _lnk_text(lnk)
    before_task = task_log.read_text(encoding="utf-8")
    assert "wscript.exe" in before_lnk.lower()
    assert before_task.split("Execute=", 1)[1].split("\n", 1)[0].strip().lower().endswith("wscript.exe")

    r2 = _run_register_supervisor(tree, "0", startup, task_log)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    after_lnk = _lnk_text(lnk)
    after_task = task_log.read_text(encoding="utf-8")
    assert "powershell.exe" in after_lnk.lower(), \
        "re-running after WSH was disabled should have repointed the Startup shortcut: %s" % after_lnk
    assert "wscript" not in after_lnk.lower()
    assert after_task.split("Execute=", 1)[1].split("\n", 1)[0].strip().lower().endswith("powershell.exe"), \
        "re-running should also have repointed the scheduled task action: %s" % after_task


def test_register_supervisor_records_autostart_decision_either_way(tmp_path):
    """Unrelated to WSH, but a cheap sanity check that this script's normal side effect
    (win\\convenience_marker.ps1's Set-ConvenienceDecision) still fires with the stub in place --
    a regression here would silently break start_all.ps1's re-provisioning logic."""
    tree = _build_tree(tmp_path)
    startup = tmp_path / "Startup"
    startup.mkdir()
    task_log = tmp_path / "task_action.txt"

    r = _run_register_supervisor(tree, "0", startup, task_log)
    assert r.returncode == 0, r.stdout + r.stderr
    marker = (tree / ".setup" / "convenience_provisioned").read_text(encoding="utf-8")
    assert "autostart=yes" in marker, marker
