# -*- coding: utf-8 -*-
r"""start_all.ps1's install-path fixes, run for real -- in a throwaway copy, never this machine.

What each group proves (IDs from the new-PC state-machine analysis):

  D3   quickstart.bat runs start_all twice while it is itself executing; a pull there rewrites the
       batch file cmd is reading by byte offset. The update check is gated on -CoreOnly AND on a
       cmd.exe parent. Proved by launching the gate from a real `cmd /c quickstart.bat`.
  D12  the daily "server is older than its code" kill ignored live runs and subpackages. Proved
       end to end: a dummy "main.py" process of the throwaway checkout, a newer file in a
       subpackage, a live run marker -- it survives; without the marker it is stopped.
  D30  both server-stopping paths ask stale_server_check.py --server-action (the post-update
       form is exercised here too) and an unreadable answer never authorises a kill.
  D11  provisioning re-creates only what the record says yes to AND is missing; the REAL
       unregister-supervisor.ps1 and make_desktop_shortcut.ps1 -Remove record "no", after which
       nothing comes back. Desktop/Startup folders are redirected into the temp dir.
  D14  a named mutex serialises start_all copies (waits, hand-over on abandon, bounded); the
       fleet resume is skipped when the supervisor just started or a coordinator is running.
  D24  a signed-out devtunnel, a missing tunnel, and a tunnel with no host are COUNTED, with the
       command to run -- against a stub devtunnel.cmd that logs its argv (read-only verbs only).
  D27  UI exes are rebuilt when missing, empty or older than a source named by the real Build
       lines, through a stub rebuild_ui.ps1 that keeps those lines.
  D29  the provisioning hint prints `scripts\register-supervisor.ps1` on one line.
  and  a failed hidden start writes .setup\logs\start_all_summary.txt and raises a notification
       through tools/notify_ops (suppressed under pytest by notify_desktop itself).

HOW. The functions are cut out of scripts/start_all.ps1 by balanced-brace extraction (as
scripts/test_a_silent_death_leaves_its_exit_code.py does) and run in a separate powershell
against a temp "checkout" assembled from the files they need. Nothing here starts the stack,
touches the live supervisor/bridge/tunnel, the real Desktop or Startup folder, or Task Scheduler
(Get-ScheduledTask / Unregister-ScheduledTask are shadowed by driver functions).

Windows-only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
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

#: What the temp checkout needs, copied from THIS working tree (a clone would miss the edits
#: under test). EVERY ENTRY HERE MUST BE TRACKED (`git ls-files`), because CI's checkout is
#: built from the git index, not from this machine's working tree: a file that exists here but
#: was never `git add`ed -- `bench/__init__.py` was exactly that, an empty package marker
#: created locally and never committed -- is silently absent on a fresh CI checkout, and
#: `shutil.copy2` then raises FileNotFoundError before a single test body runs. `bench/__init__.py`
#: is created directly by the `checkout` fixture below instead of copied, since all it has to be
#: is present and empty -- the temp tree's own need, not a copy of anything.
_COPY = ["scripts/stale_server_check.py", "scripts/win/convenience_marker.ps1",
         "scripts/unregister-supervisor.ps1", "scripts/make_desktop_shortcut.ps1",
         "scripts/start_all_hidden.vbs", "tools/__init__.py", "tools/childproc.py",
         "tools/deploy_freshness.py", "tools/notify_ops.py", "relay/fleet_reaper.py",
         "bench/ui_build_check.py"]

_FUNCS = ["Env-Value", "Get-UpdateCheckSkipReason", "Get-ParentProcessInfo",
          "Get-ThisCheckoutServerProcesses", "Get-ServerStartEpoch", "ConvertTo-ServerActionResult",
          "Get-ServerAction", "Invoke-ServerAction", "Enter-StartAllLock", "Exit-StartAllLock",
          "Exit-StartAllWaiterSlot", "Remove-StartAllRoleRecord", "Get-StartAllRolePath",
          "ConvertFrom-StartAllRoleJson",
          "Save-StartAllRunLines",
          "Get-FleetResumeSkipReason", "Get-ThisCheckoutFleetCoordinatorPids",
          "Resolve-DevTunnelExe", "Invoke-DevTunnelBounded", "Get-TunnelLoginState",
          "Get-TunnelHostCount", "Test-TunnelServing", "ConvertFrom-UiStaleLines",
          "Get-UiBuildState", "Invoke-UiStep", "Write-StartupSummary",
          "Test-ShouldNotifyStartupFailures", "Send-StartupFailureNotice",
          "Ensure-ConvenienceProvisioning", "Test-ShortcutTargetsWscript", "Test-WshDisabled",
          "Get-Win32ProcessByPid", "ConvertFrom-WmiDate", "Get-LaunchLineage", "Get-StartAllMode",
          "New-StartAllRunRecord", "Write-StartAllRunRecord"]


def _extract_braced_block(text: str, start_marker: str) -> str:
    """The balanced-brace block starting at `start_marker` (not string-aware; every brace inside
    a string in the extracted functions is itself balanced -- "@{u}", '"{0}"')."""
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


@pytest.fixture(scope="module")
def functions() -> str:
    src = open(START_ALL, encoding="utf-8").read()
    out = []
    for name in _FUNCS:
        m = re.search(r"(?m)^function %s\b" % re.escape(name), src)
        assert m, "start_all.ps1 no longer defines %s" % name
        out.append(_extract_braced_block(src, m.group(0)))
    return "\n\n".join(out)


def _q(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


@pytest.fixture()
def checkout(tmp_path):
    root = tmp_path / "co"
    for rel in _COPY:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), dst)
    for d in ("tools", "relay", ".fleet", ".setup"):
        (root / d).mkdir(exist_ok=True)
    # bench/__init__.py is not copied (see _COPY's comment): it only has to exist and be
    # empty for `bench` to import as a package, which is a fact about the temp tree, not
    # something to fetch from a source file that CI's checkout may not carry.
    (root / "bench").mkdir(exist_ok=True)
    (root / "bench" / "__init__.py").touch()
    return root


def _refused_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d/status" % port


def _driver(functions, root, body, script_dir=None, venv_py=None, bridge=None):
    pre = "\n".join([
        '$ErrorActionPreference = "Continue"',
        "$root = %s" % _q(root),
        "$scriptDir = %s" % _q(script_dir or os.path.join(root, "scripts")),
        "$script:venvPy = %s" % _q(venv_py or sys.executable),
        "$script:bridgeStatusUrl = %s" % _q(bridge or _refused_url()),
        "$script:startupFailures = @()",
        "$script:startupUnclear = @()",
        "$script:splash = $null",
        "function Set-SplashStatus($s, [string]$t) { }",
        "function Pump-Splash($s) { }",
        "function Hide-Secrets([string]$text) { return $text }",
        ". (Join-Path %s 'scripts\\win\\convenience_marker.ps1')" % _q(root),
    ])
    return pre + "\n\n" + functions + "\n\n" + body + "\n"


def _ps(tmp_path, text, env=None, timeout=240):
    p = tmp_path / ("drv_%s.ps1" % uuid.uuid4().hex[:8])
    p.write_text(text, encoding="utf-8-sig")
    r = childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                      env=env, timeout=timeout)
    return r


def _result(r):
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    raise AssertionError("no RESULT line.\nstdout:\n%s\nstderr:\n%s" % (r.stdout[-3000:], r.stderr[-3000:]))


# =============================================================== D3: the update-check gate

def test_update_gate_truth_table(tmp_path, checkout, functions):
    body = r"""
$r = @{
  noui     = (Get-UpdateCheckSkipReason -NoUi $true -CoreOnly $false -ParentName 'wscript.exe' -ParentCommandLine '');
  core     = (Get-UpdateCheckSkipReason -NoUi $false -CoreOnly $true -ParentName 'wscript.exe' -ParentCommandLine '');
  qs       = (Get-UpdateCheckSkipReason -NoUi $false -CoreOnly $false -ParentName 'cmd.exe' -ParentCommandLine 'C:\WINDOWS\system32\cmd.exe /c ""C:\x y\repo\quickstart.bat" "');
  vbs      = (Get-UpdateCheckSkipReason -NoUi $false -CoreOnly $false -ParentName 'wscript.exe' -ParentCommandLine 'wscript.exe "C:\r\scripts\start_all_hidden.vbs"');
  unknown  = (Get-UpdateCheckSkipReason -NoUi $false -CoreOnly $false -ParentName '' -ParentCommandLine '');
}
"RESULT:" + ($r | ConvertTo-Json -Compress)
"""
    r = _result(_ps(tmp_path, _driver(functions, checkout, body)))
    assert r["noui"] == "-NoUi"
    assert "-CoreOnly" in r["core"]
    assert "quickstart.bat" in r["qs"] and "rewrite" in r["qs"]
    assert r["vbs"] == "" and r["unknown"] == ""


def test_update_gate_sees_a_real_batch_parent(tmp_path, checkout, functions):
    """The mechanism, not the table: launched from `cmd /c quickstart.bat` the gate closes;
    launched from Python (as wscript would be, a non-cmd parent) it stays open."""
    body = r"""
$p = Get-ParentProcessInfo
"RESULT:" + (@{ parent = $p.Name; skip = (Get-UpdateCheckSkipReason -NoUi $false -CoreOnly $false -ParentName $p.Name -ParentCommandLine $p.CommandLine) } | ConvertTo-Json -Compress)
"""
    drv = tmp_path / "gate_driver.ps1"
    drv.write_text(_driver(functions, checkout, body), encoding="utf-8-sig")
    bat = tmp_path / "quickstart.bat"
    bat.write_text('@echo off\r\n"%s" -NoProfile -ExecutionPolicy Bypass -File "%s"\r\n' % (_POWERSHELL, drv),
                   encoding="ascii")
    via_bat = _result(childproc.run(["cmd.exe", "/c", str(bat)], timeout=240))
    assert via_bat["parent"].lower() == "cmd.exe"
    assert "quickstart.bat" in via_bat["skip"]
    direct = _result(childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                                    "-File", str(drv)], timeout=240))
    assert direct["skip"] == "", direct


# =============================================================== D12 / D30: one server rule

def _dummy_server(root):
    """A process whose command line names this checkout's main.py -- what the scan looks for."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(180)",
                             os.path.join(str(root), "main.py")])


_DAILY = r"""
$e = Get-ServerStartEpoch
$a = Get-ServerAction -StartedEpoch $e
$note = Invoke-ServerAction $a "[t]"
"RESULT:" + (@{ epoch = $e; verdict = $a.Verdict; why = @($a.Why) } | ConvertTo-Json -Compress)
"""


def test_daily_check_keeps_a_live_run_and_sees_subpackages(tmp_path, checkout, functions):
    (checkout / "main.py").write_text("# server\n", encoding="utf-8")
    os.utime(checkout / "main.py", (1_000_000, 1_000_000))
    proc = _dummy_server(checkout)
    try:
        time.sleep(2)
        sub = checkout / "tools" / "auto" / "forged.py"       # a SUBPACKAGE file
        sub.parent.mkdir(parents=True, exist_ok=True)
        sub.write_text("# new\n", encoding="utf-8")
        os.utime(sub, (time.time() + 30, time.time() + 30))
        (checkout / ".fleet" / "fleet_run_active.json").write_text(
            json.dumps({"pid": os.getpid()}), encoding="utf-8")

        live = _result(_ps(tmp_path, _driver(functions, checkout, _DAILY)))
        assert live["epoch"] > 0, "the dummy server of this checkout was not found"
        assert live["verdict"] == "report-only", live
        assert proc.poll() is None, "a live run's server was stopped"

        (checkout / ".fleet" / "fleet_run_active.json").unlink()
        idle = _result(_ps(tmp_path, _driver(functions, checkout, _DAILY)))
        assert idle["verdict"] == "swap-needed", idle
        assert any("tools/auto/forged.py" in w for w in idle["why"])
        for _ in range(20):
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        assert proc.poll() is not None, "the stale server of this checkout was not stopped"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_post_update_form_and_unreadable_answers(tmp_path, checkout, functions):
    body = r"""
$r = @{
  server = (Get-ServerAction -ChangedPaths @('tools/x.py', 'docs/a.md')).Verdict;
  docs   = (Get-ServerAction -ChangedPaths @('docs/a.md', 'ui/A.cs')).Verdict;
  crash  = (ConvertTo-ServerActionResult @('Traceback', 'boom') 1).Verdict;
  junk   = (ConvertTo-ServerActionResult @('yes') 0).Verdict;
  empty  = (ConvertTo-ServerActionResult @() 0).Verdict;
  good   = (ConvertTo-ServerActionResult @('why: a fleet run is live', 'report-only') 0);
}
$script:venvPy = 'C:\nonexistent\python.exe'
$r.novenv = (Get-ServerAction -ChangedPaths @('tools/x.py')).Verdict
$r.unknownNote = (Invoke-ServerAction @{ Verdict = 'unknown'; Why = @('x') } "[t]")
"RESULT:" + ($r | ConvertTo-Json -Compress -Depth 4)
"""
    r = _result(_ps(tmp_path, _driver(functions, checkout, body)))
    assert r["server"] == "swap-needed"
    assert r["docs"] == "noop"
    assert r["crash"] == r["junk"] == r["empty"] == r["novenv"] == "unknown"
    assert r["good"]["Verdict"] == "report-only" and r["good"]["Why"] == ["a fleet run is live"]
    assert not r["unknownNote"]


def test_post_update_form_does_not_depend_on_the_hosts_pipe_encoding(tmp_path, checkout, functions):
    """CI (windows-install-smoke, 2026-09-23) got "noop" where this machine got "swap-needed"
    for the same tools/x.py. The path list reached Python through PowerShell's native pipe,
    which encodes with the HOST's $OutputEncoding; a UTF-8 one is written with a BOM, the first
    path became "﻿tools/x.py", and a changed server read as unchanged. Reproduced here by
    giving the host exactly that encoding: the answer must not move. A non-ASCII path from
    `git diff` must survive the trip as well."""
    body = r"""
$OutputEncoding = [System.Text.Encoding]::UTF8
$a = Get-ServerAction -ChangedPaths @('tools/x.py', 'docs/a.md')
$b = Get-ServerAction -ChangedPaths @('docs/a.md', ('tools/' + [char]0x65E5 + [char]0x672C + '.py'))
$c = Get-ServerAction -ChangedPaths @('docs/a.md')
"RESULT:" + (@{ first = $a.Verdict; why = @($a.Why); nonascii = $b.Verdict; nonasciiWhy = @($b.Why); docs = $c.Verdict } | ConvertTo-Json -Compress)
"""
    r = _result(_ps(tmp_path, _driver(functions, checkout, body)))
    assert r["first"] == "swap-needed", r
    assert r["why"] == ["the update changed tools/x.py"], r
    assert r["nonascii"] == "swap-needed", r
    assert r["nonasciiWhy"] == ["the update changed tools/日本.py"], r
    assert r["docs"] == "noop", r


# =============================================================== D11 + D29: provisioning

_STUB_SCRIPT = r"""
Add-Content -Path %(log)s -Value '%(name)s'
New-Item -ItemType File -Force -Path %(lnk)s | Out-Null
"""


def test_provisioning_honours_the_recorded_answer(tmp_path, checkout, functions):
    desk, start, stubs = tmp_path / "Desktop", tmp_path / "Startup", tmp_path / "stubs"
    for d in (desk, start, stubs):
        d.mkdir()
    log = tmp_path / "calls.log"
    (stubs / "make_desktop_shortcut.ps1").write_text(_STUB_SCRIPT % {
        "log": _q(log), "name": "shortcut", "lnk": _q(desk / "M365 Companion.lnk")}, encoding="ascii")
    (stubs / "register-supervisor.ps1").write_text(_STUB_SCRIPT % {
        "log": _q(log), "name": "autostart", "lnk": _q(start / "M365 Companion.lnk")}, encoding="ascii")
    env = dict(os.environ, M365_COMPANION_DESKTOP_DIR=str(desk), M365_COMPANION_STARTUP_DIR=str(start))
    marker = checkout / ".setup" / "convenience_provisioned"

    def calls():
        return log.read_text(encoding="utf-8").split() if log.exists() else []

    def provision():
        return _ps(tmp_path, _driver(functions, checkout, "Ensure-ConvenienceProvisioning",
                                     script_dir=str(stubs)), env=env)

    # SAFETY FIRST: the redirection must hold before any real script is run.
    probe = _ps(tmp_path, _driver(functions, checkout,
                                  '"RESULT:" + (@{ d = (Get-DesktopLauncherPath); s = (Get-StartupLauncherPath) } | ConvertTo-Json -Compress)'),
                env=env)
    paths = _result(probe)
    assert paths["d"].startswith(str(tmp_path)) and paths["s"].startswith(str(tmp_path)), paths

    out = provision().stdout
    assert calls() == [], "created something with no decision on record"
    assert "scripts\\register-supervisor.ps1 to do either by hand" in out, "D29: the hint is broken over two lines"

    marker.write_text("shortcut=yes\r\nautostart=yes\r\n", encoding="ascii")
    provision()
    assert sorted(calls()) == ["autostart", "shortcut"]
    provision()
    assert sorted(calls()) == ["autostart", "shortcut"], "re-ran with both already present"

    # the REAL unregister-supervisor.ps1, with Task Scheduler shadowed
    unreg = r"""
function Get-ScheduledTask { [CmdletBinding()] param([Parameter(ValueFromRemainingArguments=$true)]$Rest) return $null }
function Unregister-ScheduledTask { [CmdletBinding()] param([Parameter(ValueFromRemainingArguments=$true)]$Rest) throw 'STUB: must not be reached' }
& %s
"RESULT:" + (@{ ok = $true } | ConvertTo-Json -Compress)
""" % _q(checkout / "scripts" / "unregister-supervisor.ps1")
    _result(_ps(tmp_path, _driver(functions, checkout, unreg), env=env))
    assert not (start / "M365 Companion.lnk").exists()
    assert "autostart=no" in marker.read_text(encoding="ascii").split()
    assert "shortcut=yes" in marker.read_text(encoding="ascii").split(), "the other answer was lost"
    provision()
    assert calls().count("autostart") == 1, "autostart came back after unregister-supervisor.ps1"
    assert not (start / "M365 Companion.lnk").exists()

    # a hand-deleted Desktop icon IS re-created while the record says yes ...
    (desk / "M365 Companion.lnk").unlink()
    provision()
    assert calls().count("shortcut") == 2
    # ... and make_desktop_shortcut.ps1 -Remove is the way to say no
    rm = "& %s -Remove\n\"RESULT:{}\"" % _q(checkout / "scripts" / "make_desktop_shortcut.ps1")
    _result(_ps(tmp_path, _driver(functions, checkout, rm), env=env))
    assert not (desk / "M365 Companion.lnk").exists()
    assert "shortcut=no" in marker.read_text(encoding="ascii").split()
    provision()
    assert calls().count("shortcut") == 2, "the Desktop launcher came back after -Remove"


def _real_wscript_shortcut(tmp_path, desktop):
    """A Desktop .lnk made by the REAL make_desktop_shortcut.ps1 while WSH works (so it targets
    wscript.exe), in a throwaway tree -- as scripts/test_shortcuts_without_wsh.py builds it."""
    tree = tmp_path / "lnk_tree"
    (tree / "scripts" / "win").mkdir(parents=True)
    for name in ("make_desktop_shortcut.ps1", "preflight_policy.ps1"):
        shutil.copyfile(os.path.join(HERE, name), tree / "scripts" / name)
    shutil.copyfile(os.path.join(HERE, "win", "convenience_marker.ps1"),
                    tree / "scripts" / "win" / "convenience_marker.ps1")
    (tree / "scripts" / "start_all_hidden.vbs").write_text("' stub\n", encoding="ascii")
    (tree / "scripts" / "start_all.ps1").write_text("# stub\n", encoding="ascii")
    env = dict(os.environ, PREFLIGHT_TEST_WSH_ENABLED="1", M365_COMPANION_DESKTOP_DIR=str(desktop))
    r = childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                       str(tree / "scripts" / "make_desktop_shortcut.ps1")], cwd=str(tree), env=env,
                      timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    lnk = desktop / "M365 Companion.lnk"
    assert lnk.is_file()
    return lnk


def test_a_launcher_left_on_wscript_is_remade_once_wsh_is_disabled(tmp_path, checkout, functions):
    """f27826d made the two launcher scripts pick powershell.exe when WSH is disabled, but
    provisioning only re-ran them for a MISSING shortcut. A shortcut made while WSH worked,
    still pointing at wscript.exe after WSH was disabled, starts nothing -- it must be re-made.
    While WSH works, or once it no longer targets wscript.exe, it is left alone."""
    desk, start, stubs = tmp_path / "Desktop", tmp_path / "Startup", tmp_path / "stubs"
    for d in (desk, start, stubs):
        d.mkdir()
    _real_wscript_shortcut(tmp_path, desk)
    # The Startup one as bytes: a wide-string target the way a .lnk stores it.
    (start / "M365 Companion.lnk").write_bytes(
        b"L\x00\x00\x00" + "C:\\Windows\\System32\\wscript.exe".encode("utf-16-le") + b"\x00" * 8)
    log = tmp_path / "calls.log"
    (stubs / "make_desktop_shortcut.ps1").write_text(_STUB_SCRIPT % {
        "log": _q(log), "name": "shortcut", "lnk": _q(desk / "M365 Companion.lnk")}, encoding="ascii")
    (stubs / "register-supervisor.ps1").write_text(_STUB_SCRIPT % {
        "log": _q(log), "name": "autostart", "lnk": _q(start / "M365 Companion.lnk")}, encoding="ascii")
    shutil.copyfile(os.path.join(HERE, "preflight_policy.ps1"), stubs / "preflight_policy.ps1")
    (checkout / ".setup" / "convenience_provisioned").write_text("shortcut=yes\r\nautostart=yes\r\n",
                                                                 encoding="ascii")

    def calls():
        return sorted(log.read_text(encoding="utf-8").split()) if log.exists() else []

    def provision(wsh):
        env = dict(os.environ, M365_COMPANION_DESKTOP_DIR=str(desk),
                   M365_COMPANION_STARTUP_DIR=str(start), PREFLIGHT_TEST_WSH_ENABLED=wsh)
        return _ps(tmp_path, _driver(functions, checkout, "Ensure-ConvenienceProvisioning",
                                     script_dir=str(stubs)), env=env).stdout

    probe = _driver(functions, checkout, '"RESULT:" + (@{ d = (Test-ShortcutTargetsWscript %s); '
                    's = (Test-ShortcutTargetsWscript %s) } | ConvertTo-Json -Compress)'
                    % (_q(desk / "M365 Companion.lnk"), _q(start / "M365 Companion.lnk")))
    assert _result(_ps(tmp_path, probe)) == {"d": True, "s": True}, "the real .lnk's target was not read"

    provision("1")
    assert calls() == [], "re-made a wscript launcher while WSH works"
    out = provision("0")
    assert calls() == ["autostart", "shortcut"], calls()
    assert "Windows Script Host is disabled" in out, out
    provision("0")
    assert calls() == ["autostart", "shortcut"], "re-made a launcher that no longer targets wscript.exe"


def test_a_legacy_marker_is_left_alone_and_survives_a_rewrite(tmp_path, checkout, functions):
    marker = checkout / ".setup" / "convenience_provisioned"
    marker.write_text("provisioned\r\n", encoding="ascii")
    body = r"""
$before = Read-ConvenienceDecision $root
$ok = Set-ConvenienceDecision $root 'autostart' 'no'
$plan = Get-ConvenienceProvisioningPlan -Decision (Read-ConvenienceDecision $root) -ShortcutPresent $false -AutostartPresent $false
"RESULT:" + (@{ before = $before.Count; ok = $ok; s = $plan.Shortcut; a = $plan.Autostart } | ConvertTo-Json -Compress)
"""
    r = _result(_ps(tmp_path, _driver(functions, checkout, body)))
    assert r == {"before": 0, "ok": True, "s": False, "a": False}
    assert marker.read_text(encoding="ascii").split() == ["provisioned", "autostart=no"]


# =============================================================== D14: one start_all at a time

_HOLD = r"""
$got = Enter-StartAllLock -Name %(name)s -TimeoutSec %(timeout)d
$t0 = [DateTime]::UtcNow.Ticks
if ($got) { Start-Sleep -Milliseconds %(hold)d }
$t1 = [DateTime]::UtcNow.Ticks
%(tail)s
"RESULT:" + (@{ got = $got; t0 = $t0; t1 = $t1 } | ConvertTo-Json -Compress)
"""


def _hold(tmp_path, checkout, functions, name, hold_ms, timeout=60, tail="Exit-StartAllLock"):
    p = tmp_path / ("hold_%s.ps1" % uuid.uuid4().hex[:8])
    p.write_text(_driver(functions, checkout, _HOLD % {
        "name": _q(name), "timeout": timeout, "hold": hold_ms, "tail": tail}), encoding="utf-8-sig")
    return subprocess.Popen([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _out(proc):
    so, se = proc.communicate(timeout=240)
    return _result(type("R", (), {"stdout": childproc.decode(so), "stderr": childproc.decode(se)}))


def test_two_copies_run_one_after_the_other(tmp_path, checkout, functions):
    name = "Global\\m365-test-start-all-%s" % uuid.uuid4().hex
    a = _hold(tmp_path, checkout, functions, name, 4000)
    time.sleep(1.5)
    b = _hold(tmp_path, checkout, functions, name, 500)
    ra, rb = _out(a), _out(b)
    assert ra["got"] and rb["got"], "the second copy gave up instead of waiting"
    assert rb["t0"] >= ra["t1"], "the two copies overlapped"


def test_an_abandoned_lock_is_taken_over_and_a_held_one_times_out(tmp_path, checkout, functions):
    name = "Global\\m365-test-start-all-%s" % uuid.uuid4().hex
    dead = _hold(tmp_path, checkout, functions, name, 200, tail="[Environment]::Exit(0)")
    dead.communicate(timeout=240)                     # exited holding it
    after = _out(_hold(tmp_path, checkout, functions, name, 100, timeout=5))
    assert after["got"], "an abandoned lock was not taken over"

    holder = _hold(tmp_path, checkout, functions, name, 8000)
    time.sleep(2)
    waiter = _out(_hold(tmp_path, checkout, functions, name, 100, timeout=1))
    assert waiter["got"] is False, "a held lock did not time out"
    assert _out(holder)["got"]


def test_fleet_resume_is_not_a_second_resumer(tmp_path, checkout, functions):
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                              "-m", "relay.fleet_runner", str(checkout)])
    try:
        time.sleep(2)
        body = r"""
$pids = Get-ThisCheckoutFleetCoordinatorPids
$r = @{
  pids     = @($pids);
  running  = (Get-FleetResumeSkipReason -SupervisorJustStarted $false -RunningCoordinatorPids $pids -AutoResumeSetting '');
  fresh    = (Get-FleetResumeSkipReason -SupervisorJustStarted $true -RunningCoordinatorPids @() -AutoResumeSetting '');
  optout   = (Get-FleetResumeSkipReason -SupervisorJustStarted $false -RunningCoordinatorPids @() -AutoResumeSetting 'off');
  go       = (Get-FleetResumeSkipReason -SupervisorJustStarted $false -RunningCoordinatorPids @() -AutoResumeSetting '');
}
"RESULT:" + ($r | ConvertTo-Json -Compress)
"""
        r = _result(_ps(tmp_path, _driver(functions, checkout, body)))
    finally:
        dummy.kill()
    assert dummy.pid in r["pids"]
    assert "already running" in r["running"]
    assert "supervisor" in r["fresh"]
    assert "MCP_FLEET_AUTORESUME" in r["optout"]
    assert r["go"] == ""


# =============================================================== D24: the tunnel is counted

_STUB_DT = "\r\n".join([
    "@echo off",
    'echo %*>> "%STUB_LOG%"',
    'if "%1"=="user" goto user',
    'if "%1"=="show" goto show',
    "exit /b 0",
    ":user",
    'if "%STUB_LOGIN%"=="out" goto out',
    'if "%STUB_LOGIN%"=="weird" goto userweird',
    "echo Logged in as test@x using Microsoft.",
    "exit /b 0",
    ":out",
    "echo Not logged in.",
    "exit /b 1",
    ":userweird",
    "echo some unrecognised CLI output that names neither state",
    "exit /b 0",
    ":show",
    'if "%STUB_SHOW%"=="missing" goto missing',
    'if "%STUB_SHOW%"=="weird" goto showweird',
    "echo Tunnel ID             : t1.jpe1",
    "echo Host connections      : %STUB_HOSTS%",
    "exit /b 0",
    ":missing",
    "echo Tunnel not found.",
    "exit /b 1",
    ":showweird",
    "echo some unrecognised CLI output that names neither state",
    "exit /b 0",
    ""])


@pytest.mark.parametrize("login,show,hosts,envline,expect", [
    ("out", "ok", "1", "MCP_TUNNEL_NAME=t1", "devtunnel user login"),
    ("in", "missing", "0", "MCP_TUNNEL_NAME=t1", "setup_devtunnel.ps1"),
    ("in", "ok", "0", "MCP_TUNNEL_NAME=t1", "is not hosted"),
    ("in", "ok", "1", "MCP_TUNNEL_NAME=", "MCP_TUNNEL_NAME is empty"),
    ("in", "ok", "2", "MCP_TUNNEL_NAME=t1", None),
    ("nocli", "ok", "1", "MCP_TUNNEL_NAME=t1", "devtunnel CLI not found"),
])
def test_the_tunnel_is_part_of_started(tmp_path, checkout, functions, login, show, hosts, envline, expect):
    bin_dir, lad = tmp_path / "bin", tmp_path / "localappdata"
    bin_dir.mkdir()
    lad.mkdir()
    if login != "nocli":
        (bin_dir / "devtunnel.cmd").write_text(_STUB_DT, encoding="ascii")
    (checkout / ".env").write_text(envline + "\r\n", encoding="ascii")
    stub_log = tmp_path / "devtunnel_argv.log"
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    # The stub first. For "no CLI", NOTHING else: an IT-deployed devtunnel.exe can sit in
    # System32 (it does on the machine this was written on), and the test must never reach a
    # real CLI.
    path = [str(bin_dir)]
    if login != "nocli":
        path += [os.path.join(sysroot, "System32"), sysroot]
    env = dict(os.environ, STUB_LOG=str(stub_log), STUB_LOGIN=login, STUB_SHOW=show, STUB_HOSTS=hosts,
               LOCALAPPDATA=str(lad), PATH=";".join(path))
    body = r"""
Test-TunnelServing -HostWaitSec 2 -PollSec 1
"RESULT:" + (@{ failures = @($script:startupFailures); unclear = @($script:startupUnclear) } | ConvertTo-Json -Compress)
"""
    r = _result(_ps(tmp_path, _driver(functions, checkout, body), env=env))
    if expect is None:
        assert r["failures"] == [], r
    else:
        assert len(r["failures"]) == 1 and expect in r["failures"][0], r
    assert r["unclear"] == [], "a definite answer must not also be reported as unclear: %r" % r
    if stub_log.exists():
        verbs = {l.split()[0] + (" " + l.split()[1] if l.split()[0] == "user" else "")
                 for l in stub_log.read_text(encoding="ascii", errors="replace").splitlines() if l.strip()}
        assert verbs <= {"user show", "show"}, "a devtunnel verb that changes something: %s" % verbs


@pytest.mark.parametrize("login,show,envline", [
    # sandbox finding (2026-09-24): `devtunnel user show` timing out / answering unreadably used
    # to be silently dropped -- not in $script:startupFailures, not in the summary, not in the
    # jsonl outcome, which read plain "ok". Neither branch may join $script:startupFailures (a
    # CLI hiccup is not a KNOWN-bad state), but neither may vanish either.
    ("weird", "ok", "MCP_TUNNEL_NAME=t1"),   # `user show` answers unreadably -> Get-TunnelLoginState "unknown"
    ("in", "weird", "MCP_TUNNEL_NAME=t1"),   # `show` answers unreadably -> Get-TunnelHostCount -2
])
def test_an_unreadable_tunnel_answer_is_reported_as_unclear_not_silently_ok(
        tmp_path, checkout, functions, login, show, envline):
    bin_dir, lad = tmp_path / "bin", tmp_path / "localappdata"
    bin_dir.mkdir()
    lad.mkdir()
    (bin_dir / "devtunnel.cmd").write_text(_STUB_DT, encoding="ascii")
    (checkout / ".env").write_text(envline + "\r\n", encoding="ascii")
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    path = [str(bin_dir), os.path.join(sysroot, "System32"), sysroot]
    env = dict(os.environ, STUB_LOG=str(tmp_path / "devtunnel_argv.log"), STUB_LOGIN=login,
               STUB_SHOW=show, STUB_HOSTS="1", LOCALAPPDATA=str(lad), PATH=";".join(path))
    body = r"""
Test-TunnelServing -HostWaitSec 2 -PollSec 1
"RESULT:" + (@{ failures = @($script:startupFailures); unclear = @($script:startupUnclear) } | ConvertTo-Json -Compress)
"""
    r = _result(_ps(tmp_path, _driver(functions, checkout, body), env=env))
    assert r["failures"] == [], "an unreadable CLI answer must not be a KNOWN-bad failure: %r" % r
    assert len(r["unclear"]) == 1 and "cannot confirm the tunnel is served" in r["unclear"][0], r


def test_unclear_startup_state_is_not_reported_as_outcome_ok(tmp_path, checkout, functions):
    """The run record (start_all_runs.jsonl) and the hidden-run summary must say plainly that
    something was never confirmed, not "ok" -- that silent "ok" is exactly what let a sandbox
    run pass with the tunnel never actually hosted (defect 2, 2026-09-24 sandbox review)."""
    summary = checkout / ".setup" / "logs" / "start_all_summary.txt"
    body = r"""
$script:startupUnclear = @('could not tell whether devtunnel is signed in (no answer within 10 s): cannot confirm the tunnel is served -- run doctor.bat')
$summaryWritten = Write-StartupSummary %s @($script:startupFailures) 'background (-NoUi)' @($script:startupUnclear)
$runOutcome = $(if ($script:startupFailures.Count -gt 0) { "failures" } elseif (@($script:startupUnclear).Count -gt 0) { "unclear" } else { "ok" })
"RESULT:" + (@{ written = $summaryWritten; outcome = $runOutcome } | ConvertTo-Json -Compress)
""" % _q(summary)
    r = _result(_ps(tmp_path, _driver(functions, checkout, body)))
    assert r == {"written": True, "outcome": "unclear"}
    lines = summary.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "failures=0"
    assert "unclear=1" in lines
    assert any(l.startswith("? could not tell whether devtunnel is signed in") for l in lines), lines


# =============================================================== D27: UI exes vs their sources

def _ui_checkout(checkout):
    from bench.ui_build_check import targets_from_rebuild_script
    ui = checkout / "ui"
    ui.mkdir()
    real = open(os.path.join(REPO, "ui", "rebuild_ui.ps1"), encoding="utf-8").read()
    build_lines = [l for l in real.splitlines() if re.match(r'\s*Build\s+"', l)]
    assert build_lines
    stub = "\r\n".join([
        "param([switch]$NoLaunch)",
        "Add-Content -Path (Join-Path $PSScriptRoot 'rebuild.log') -Value ('NoLaunch=' + $NoLaunch)",
        "if ($env:STUB_REBUILD_FAIL) { Write-Host 'BUILD FAILED: CopilotChat'; Write-Host 'error CS0103: stub'; exit 1 }",
        "function Build($name, $sources) { [IO.File]::WriteAllBytes((Join-Path $PSScriptRoot ($name + '.exe')), [byte[]](77, 90)) }",
    ] + build_lines) + "\r\n"
    (ui / "rebuild_ui.ps1").write_text(stub, encoding="ascii")
    targets = targets_from_rebuild_script()
    for _n, srcs in targets:
        for s in srcs:
            (ui / s).write_text("//\n", encoding="ascii")
            os.utime(ui / s, (1_000_000, 1_000_000))
    return ui, [n for n, _ in targets]


_UI_BODY = r"""
function Get-Process { [CmdletBinding()] param([Parameter(ValueFromRemainingArguments=$true)]$Rest) }
function Start-Process { [CmdletBinding()] param([Parameter(Position=0)]$FilePath, [Parameter(ValueFromRemainingArguments=$true)]$Rest) Add-Content -Path (Join-Path $root 'launch.log') -Value ([string]$FilePath + '|' + [string]$env:M365_LAUNCHED_BY_START_ALL) }
Invoke-UiStep
"RESULT:" + (@{ failures = @($script:startupFailures); flagAfter = [string]$env:M365_LAUNCHED_BY_START_ALL } | ConvertTo-Json -Compress)
"""


def _ui_run(tmp_path, checkout, functions, env=None, venv_py=None):
    for f in ("ui/rebuild.log", "launch.log"):
        if (checkout / f).exists():
            (checkout / f).unlink()
    r = _result(_ps(tmp_path, _driver(functions, checkout, _UI_BODY, venv_py=venv_py), env=env))
    rebuilt = (checkout / "ui" / "rebuild.log").exists()
    launched = ((checkout / "launch.log").read_text(encoding="utf-8").split("\n")
                if (checkout / "launch.log").exists() else [])
    launched = [l.strip() for l in launched if l.strip()]
    # A window start_all opens is told so (M365_LAUNCHED_BY_START_ALL=1 in ITS environment), and
    # start_all's own environment does not keep the flag for anything it launches afterwards.
    assert all(l.rsplit("|", 1)[1] == "1" for l in launched), launched
    assert r["flagAfter"] == "", "M365_LAUNCHED_BY_START_ALL leaked past the UI launch"
    return r["failures"], rebuilt, [os.path.basename(l.rsplit("|", 1)[0]) for l in launched]


def test_ui_is_rebuilt_when_missing_empty_or_older(tmp_path, checkout, functions):
    ui, names = _ui_checkout(checkout)
    fails, rebuilt, launched = _ui_run(tmp_path, checkout, functions)
    assert rebuilt and fails == [] and sorted(launched) == sorted(n + ".exe" for n in names)
    assert (ui / "rebuild.log").read_text(encoding="ascii").strip() == "NoLaunch=True"

    later = time.time() + 60
    for n in names:
        os.utime(ui / (n + ".exe"), (later, later))
    fails, rebuilt, _ = _ui_run(tmp_path, checkout, functions)
    assert not rebuilt and fails == [], "rebuilt exes that were newer than every source"

    (ui / (names[0] + ".exe")).write_bytes(b"")
    fails, rebuilt, _ = _ui_run(tmp_path, checkout, functions)
    assert rebuilt, "a zero-length exe was trusted"

    for n in names:
        os.utime(ui / (n + ".exe"), (later, later))
    src = ui / "Theme.cs"
    assert src.exists(), "Theme.cs is no longer in a Build line; pick another shared source"
    os.utime(src, (later + 60, later + 60))
    fails, rebuilt, _ = _ui_run(tmp_path, checkout, functions)
    assert rebuilt, "an exe older than one of its sources was trusted"


def test_a_failed_rebuild_is_counted(tmp_path, checkout, functions):
    ui, names = _ui_checkout(checkout)
    env = dict(os.environ, STUB_REBUILD_FAIL="1")
    fails, rebuilt, launched = _ui_run(tmp_path, checkout, functions, env=env)
    assert rebuilt
    assert sorted(fails) == sorted("%s: rebuild failed" % n for n in names)
    assert launched == [], "launched an exe that does not exist"


def test_without_venv_it_still_catches_an_empty_exe(tmp_path, checkout, functions):
    ui, names = _ui_checkout(checkout)
    for n in names:
        (ui / (n + ".exe")).write_bytes(b"MZ")
    (ui / (names[0] + ".exe")).write_bytes(b"")
    _f, rebuilt, _l = _ui_run(tmp_path, checkout, functions, venv_py=r"C:\nonexistent\python.exe")
    assert rebuilt


# =============================================================== the silent launcher

def test_a_failed_hidden_start_is_written_down_and_announced(tmp_path, checkout, functions):
    summary = checkout / ".setup" / "logs" / "start_all_summary.txt"
    body = r"""
$ok = Write-StartupSummary %s @('devtunnel is signed out: run devtunnel user login', 'bridge: x') 'background (-NoUi)'
$r = @{
  written = $ok;
  sent    = (Send-StartupFailureNotice %s);
  hidden  = (Test-ShouldNotifyStartupFailures 2 $true $true);
  console = (Test-ShouldNotifyStartupFailures 2 $false $true);
  novis   = (Test-ShouldNotifyStartupFailures 2 $false $false);
  clean   = (Test-ShouldNotifyStartupFailures 0 $true $false);
}
"RESULT:" + ($r | ConvertTo-Json -Compress)
""" % (_q(summary), _q(summary))
    # PYTEST_CURRENT_TEST is inherited, so notify_desktop returns before any toast is raised --
    # what is proved is that the code it runs imports, reads the file and exits 0.
    r = _result(_ps(tmp_path, _driver(functions, checkout, body)))
    assert r == {"written": True, "sent": True, "hidden": True, "console": False,
                 "novis": True, "clean": False}
    lines = summary.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "failures=2" and lines[2] == "mode=background (-NoUi)"
    assert "- devtunnel is signed out: run devtunnel user login" in lines
    assert lines[-1].startswith("fix: run doctor.bat")

    # a clean start rewrites it, so yesterday's list does not linger
    body2 = "$ok = Write-StartupSummary %s @() 'full'\n\"RESULT:{}\"" % _q(summary)
    _result(_ps(tmp_path, _driver(functions, checkout, body2)))
    assert summary.read_text(encoding="utf-8").splitlines()[0] == "failures=0"


# =============================================================== who started this run

def test_every_run_records_who_started_it_and_the_file_stays_bounded(tmp_path, checkout, functions):
    """2026-09-24: repeated full start_alls could not be attributed -- their launcher (wscript)
    had exited and nothing had written it down. Each run appends one line to
    .setup\\logs\\start_all_runs.jsonl: its lineage (read at the top of the script), switches,
    lock wait and outcome; the file keeps the last 500 lines."""
    runs = checkout / ".setup" / "logs" / "start_all_runs.jsonl"
    runs.parent.mkdir(parents=True, exist_ok=True)
    runs.write_text("".join('{"old":%d}\n' % i for i in range(600)), encoding="utf-8")
    body = r"""
$NoUi = $true
$NoSplash = $true
$script:runStartedAt = Get-Date
$script:launch = Get-LaunchLineage
$script:lockState = "got after waiting"
$script:lockWaitSec = 12.5
$script:startupFailures = @('a', 'b')
$ok = Write-StartAllRunRecord %s (New-StartAllRunRecord "failures")
"RESULT:" + (@{ ok = $ok } | ConvertTo-Json -Compress)
""" % _q(runs)
    assert _result(_ps(tmp_path, _driver(functions, checkout, body)))["ok"] is True
    lines = runs.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 500, len(lines)
    assert lines[0] == '{"old":101}', "the oldest lines were not the ones dropped"
    rec = json.loads(lines[-1])
    assert rec["parent_pid"] == os.getpid() and rec["parent_name"].lower().startswith("python"), rec
    assert "pytest" in rec["parent_cmd"], rec
    assert rec["grandparent_pid"] > 0 and rec["grandparent_name"], rec
    assert rec["mode"] == "background (-NoUi)" and rec["switches"] == ["-NoUi", "-NoSplash"], rec
    assert rec["lock"] == "got after waiting" and rec["lock_wait_s"] == 12.5, rec
    assert rec["failures"] == 2 and rec["outcome"] == "failures", rec
    assert rec["pid"] > 0 and rec["ts"] and rec["end"] and rec["reexec"] is False, rec


def test_the_run_record_is_wired_where_the_launcher_is_still_alive(tmp_path):
    """The lineage must be read BEFORE the splash and Invoke-Startup (the launcher exits within
    a second); the record is written at the end before the lock is released, and on the
    self-update re-exec path, which never reaches the end."""
    src = open(START_ALL, encoding="utf-8").read()
    # THE ORDER CHANGED ON PURPOSE (2026-09-24). This used to require the capture textually
    # above `function Invoke-Startup`, i.e. near the top of the script. The property that
    # protected is "read while the launcher is still alive": BEFORE the splash is built and
    # Invoke-Startup runs, because the launcher (wscript) exits within a second. It still holds:
    # the capture now sits right after the entry decision (Invoke-StartAllEntry); before it the
    # script only defines functions and probes the lock, and between it and the splash
    # ($ranViaSplash) only function definitions and dot-sources run. It moved because the lookup
    # costs ~0.65 s under load, and every copy that finds a startup running (ten clicks) paid it
    # before it could ask the lock, and the running
    # startup paid it before writing the holder record the others wait for. A copy that leaves
    # reads its own parent (-ParentOnly) right after deciding, before its record is written.
    entry = src.index("$script:entryAction = Invoke-StartAllEntry")
    capture = src.index("$script:launch = Get-LaunchLineage\n", entry)
    assert entry < capture < src.index("$ranViaSplash = $false")
    # The entry decision itself runs near the TOP (above the dot-sources and ~2,000 lines of
    # definitions it does not need), so a copy that leaves pays for none of them.
    assert entry < src.index("function Invoke-Startup") and entry < src.index('"tunnel_name_util.ps1")')
    leave = src[entry:capture]
    assert "Get-LaunchLineage -ParentOnly" in leave and leave.index("Get-LaunchLineage -ParentOnly") < leave.index("Invoke-StartAllLeave")
    top = src[:src.index("function Hide-Secrets")]
    assert "$script:launch = Get-LaunchLineage" not in top, "the lookup is back ahead of the lock probe"
    tail = src[src.index("$summaryWritten = Write-StartupSummary"):]
    assert tail.index("Write-StartAllRunRecord") < tail.index("Exit-StartAllLock")
    reexec = _extract_braced_block(src, "function Invoke-PostUpdateTail")
    assert reexec.index("New-StartAllRunRecord \"re-exec after update\"") < reexec.index("[System.Environment]::Exit(0)")
    lock = _extract_braced_block(src, "function Invoke-Startup")
    t0 = lock.index("$lockT0 = Get-Date")
    enter = lock.index("Enter-StartAllLock", t0)
    measured = re.search(r"\$script:lockWaitSec\s*=\s*\[math\]::Round\(\(\(Get-Date\) - \$lockT0\)", lock)
    state = re.search(r"\$script:lockState\s*=", lock)
    assert measured and state, "the lock wait / lock outcome is not recorded"
    assert t0 < enter < measured.start() < lock.index("if (-not $gotLock)")
