# -*- coding: utf-8 -*-
"""The PowerShell helpers added for the new-PC install path (review of 2026-09-24), executed.

  D18 preflight_policy.ps1 -- the decision function over every combination that matters, and
      the whole script run for real against a throwaway folder whose scripts carry a
      Mark-of-the-Web stream (then unblocked).
  D10 detect_proxy.ps1 -- WinINET / PAC / WinHTTP parsing and precedence.
  D21 quickstart_lock.ps1 -- acquire / refuse / take over a stale lock / release, with real PIDs.
  D28/D29 configure_env.ps1 -- Read-EnvLines reads UTF-8, Write-EnvFileAtomic swaps the file in
      and round-trips non-ASCII text unchanged.

Functions are extracted from the live .ps1 files by balanced braces and run in an isolated
powershell.exe; nothing here touches the repository's own .env or .setup.
Windows-only (PowerShell 5.1, NTFS alternate data streams).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# The harness lives under tests/ so tools/unreached.py counts it as test support (a helper
# under scripts/ read as ten unreached PRODUCTION functions and grew the burndown inventory).
sys.path.insert(0, str(HERE.parent / "tests"))

import _install_path_harness as H  # noqa: E402

pytestmark = pytest.mark.skipif(not H.IS_WINDOWS, reason="PowerShell 5.1 / NTFS only")


def _ps_str(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


# ---- D18: preflight decision ---------------------------------------------------------------

_PF = None


def _findings(tmp_path, **kw):
    global _PF
    if _PF is None:
        _PF = H.ps_functions("scripts/preflight_policy.ps1", "Get-PolicyFindings",
                             "Get-PreflightExitCode")
    args = dict(ProbeRan="$true", ProbeLanguage="'FullLanguage'", ProbeError="''",
                MachinePolicy="'Undefined'", UserPolicy="'Undefined'", MotwCount="0",
                WshEnabled="$true", Root="'C:\\x\\repo'")
    args.update(kw)
    call = "Get-PolicyFindings " + " ".join("-%s %s" % (k, v) for k, v in args.items())
    drv = _PF + "\n$f = " + call + "\n" + \
        "foreach ($x in $f) { Write-Output ('ID=' + $x.Id); foreach ($l in $x.Lines) { Write-Output $l } }\n" + \
        "Write-Output ('EXIT=' + (Get-PreflightExitCode $f))\n"
    r = H.run_ps(drv, tmp_path)
    assert r.returncode == 0, r.stderr
    ids = [l[3:] for l in r.stdout.splitlines() if l.startswith("ID=")]
    code = int([l for l in r.stdout.splitlines() if l.startswith("EXIT=")][0][5:])
    return ids, code, r.stdout


def test_nothing_in_the_way_is_silent(tmp_path):
    ids, code, _ = _findings(tmp_path)
    assert ids == [] and code == 0


def test_mark_of_the_web_under_a_remotesigned_policy_blocks_and_is_fixable(tmp_path):
    ids, code, out = _findings(tmp_path, ProbeRan="$false", ProbeLanguage="''",
                               ProbeError="'File x.ps1 cannot be loaded. The file is not digitally signed.'",
                               MachinePolicy="'RemoteSigned'", MotwCount="12")
    assert ids == ["motw-blocking"] and code == 4
    assert "Unblock-File" in out and "NEXT STEP" in out and "MachinePolicy" in out


def test_a_group_policy_allsigned_is_named_with_the_next_step(tmp_path):
    ids, code, out = _findings(tmp_path, ProbeRan="$false", ProbeLanguage="''",
                               ProbeError="'running scripts is disabled'",
                               UserPolicy="'AllSigned'")
    assert ids == ["gpo-policy"] and code == 1
    assert "AllSigned" in out and "UserPolicy" in out and "IT" in out


def test_constrained_language_mode_stops_with_the_reason(tmp_path):
    ids, code, out = _findings(tmp_path, ProbeLanguage="'ConstrainedLanguage'")
    assert ids == ["clm"] and code == 1
    assert "AppLocker" in out and "SETUP_IGNORE_POLICY" in out


def test_a_mark_that_does_not_block_is_offered_not_forced(tmp_path):
    ids, code, _ = _findings(tmp_path, MotwCount="3")
    assert ids == ["motw-present"] and code == 3


def test_wsh_disabled_is_a_warning_with_a_way_round(tmp_path):
    ids, code, out = _findings(tmp_path, WshEnabled="$false")
    assert ids == ["wsh-disabled"] and code == 0
    assert "start_all.ps1" in out


def test_an_unexplained_refusal_quotes_powershell(tmp_path):
    ids, code, out = _findings(tmp_path, ProbeRan="$false", ProbeLanguage="''",
                               ProbeError="'Something odd happened XYZZY'")
    assert ids == ["probe-refused"] and code == 1 and "XYZZY" in out


# ---- D18: the real script, end to end ------------------------------------------------------

def _preflight_tree(tmp_path) -> Path:
    tree = tmp_path / "pf repo (1)"
    (tree / "scripts" / "win").mkdir(parents=True)
    shutil.copyfile(H.REPO / "scripts" / "preflight_policy.ps1", tree / "scripts" / "preflight_policy.ps1")
    shutil.copyfile(H.REPO / "scripts" / "win" / "wsh_vbs_check.ps1",
                     tree / "scripts" / "win" / "wsh_vbs_check.ps1")
    (tree / "scripts" / "other.ps1").write_text("Write-Output ok\n", encoding="ascii")
    (tree / "start.bat").write_text("@echo off\r\n", encoding="ascii")
    return tree


def _run_preflight(tree: Path):
    return H.childproc.run(
        [H.POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         "$PreflightRoot = (Get-Location).Path; iex (Get-Content -Raw -LiteralPath 'scripts\\preflight_policy.ps1')"],
        cwd=str(tree), timeout=120)


def _machine_policy_is_clear():
    r = H.childproc.run([H.POWERSHELL, "-NoProfile", "-Command",
                         "(Get-ExecutionPolicy -Scope MachinePolicy).ToString() + ',' + "
                         "(Get-ExecutionPolicy -Scope UserPolicy).ToString() + ',' + "
                         "$ExecutionContext.SessionState.LanguageMode"], timeout=60)
    return r.stdout.strip() == "Undefined,Undefined,FullLanguage"


def test_the_real_preflight_finds_a_mark_and_unblock_clears_it(tmp_path):
    if not _machine_policy_is_clear():
        pytest.skip("this machine has a policy set; the end-to-end case assumes none")
    tree = _preflight_tree(tmp_path)
    r = _run_preflight(tree)
    assert r.returncode == 0, (r.stdout, r.stderr)
    # Put the Zone.Identifier stream Windows writes on files extracted from a downloaded ZIP.
    mark = ("Set-Content -LiteralPath %s -Stream Zone.Identifier -Value "
            "\"[ZoneTransfer]`r`nZoneId=3\"" % _ps_str(tree / "scripts" / "other.ps1"))
    assert H.childproc.run([H.POWERSHELL, "-NoProfile", "-Command", mark], timeout=60).returncode == 0
    r = _run_preflight(tree)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "Mark-of-the-Web" in r.stdout and "1 script file" in r.stdout
    # The exact remedy setup.bat runs.
    H.childproc.run([H.POWERSHELL, "-NoProfile", "-Command",
                     "Get-ChildItem -LiteralPath . -Recurse -File -Force | Unblock-File"],
                    cwd=str(tree), timeout=60)
    r = _run_preflight(tree)
    assert r.returncode == 0, (r.stdout, r.stderr)


# ---- D10: proxy detection ------------------------------------------------------------------

_PX = None


def _proxy(tmp_path, expr: str) -> str:
    global _PX
    if _PX is None:
        _PX = H.ps_functions("scripts/detect_proxy.ps1", "ConvertTo-ProxyUrl",
                             "Get-WinHttpProxyFromBytes", "Select-SystemProxy")
    r = H.run_ps(_PX + "\nWrite-Output ('[' + (" + expr + ") + ']')\n", tmp_path)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().splitlines()[-1][1:-1]


def test_a_plain_host_port_becomes_a_url(tmp_path):
    assert _proxy(tmp_path, "ConvertTo-ProxyUrl 'proxy.corp:8080'") == "http://proxy.corp:8080"


def test_the_https_entry_of_a_per_scheme_list_wins(tmp_path):
    assert _proxy(tmp_path, "ConvertTo-ProxyUrl 'http=a:1;https=b:2;ftp=c:3'") == "http://b:2"
    assert _proxy(tmp_path, "ConvertTo-ProxyUrl 'http=a:1;ftp=c:3'") == "http://a:1"
    assert _proxy(tmp_path, "ConvertTo-ProxyUrl 'socks=s:1080'") == ""


def test_winhttp_settings_bytes_are_parsed_locale_independently(tmp_path):
    proxy = b"wh.corp:3128"
    blob = bytes([0x28, 0, 0, 0, 0, 0, 0, 0, 3, 0, 0, 0, len(proxy), 0, 0, 0]) + proxy + bytes(4)
    arr = "@(" + ",".join(str(b) for b in blob) + ")"
    assert _proxy(tmp_path, "Get-WinHttpProxyFromBytes " + arr) == "wh.corp:3128"
    direct = bytes([0x28, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    assert _proxy(tmp_path, "Get-WinHttpProxyFromBytes @(" + ",".join(map(str, direct)) + ")") == ""


def test_precedence_wininet_then_pac_then_winhttp(tmp_path):
    q = "Select-SystemProxy -WinInetEnable '%s' -WinInetServer '%s' -PacProxy '%s' -WinHttpServer '%s'"
    assert _proxy(tmp_path, q % ("1", "ie:1", "http://pac:2", "wh:3")) == "http://ie:1"
    assert _proxy(tmp_path, q % ("0", "ie:1", "http://pac:2", "wh:3")) == "http://pac:2"
    assert _proxy(tmp_path, q % ("0", "", "", "wh:3")) == "http://wh:3"
    assert _proxy(tmp_path, q % ("0", "", "", "")) == ""


def test_the_real_script_runs_and_prints_at_most_one_url(tmp_path):
    r = H.childproc.run([H.POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                         str(H.REPO / "scripts" / "detect_proxy.ps1")], timeout=120)
    assert r.returncode == 0, r.stderr
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) <= 1 and all(l.startswith(("http://", "https://")) for l in lines)


# ---- D21: the quickstart lock --------------------------------------------------------------

LOCK = H.REPO / "scripts" / "quickstart_lock.ps1"


def _lock(action, lockpath, owner):
    return H.childproc.run([H.POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(LOCK), action, "-LockPath", str(lockpath), "-OwnerPid", str(owner)],
                           timeout=120)


def _sleeper():
    return subprocess.Popen(["cmd", "/c", "ping -n 60 127.0.0.1 >nul"],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def test_a_second_quickstart_is_refused_while_the_first_runs(tmp_path):
    lock = tmp_path / ".setup" / "quickstart.lock"
    a, b = _sleeper(), _sleeper()
    try:
        assert _lock("acquire", lock, a.pid).returncode == 0
        assert lock.is_file()
        r = _lock("acquire", lock, b.pid)
        assert r.returncode == 10, (r.stdout, r.stderr)
        assert "ALREADY RUNNING" in r.stdout and str(a.pid) in r.stdout
        # The same console re-running after an interrupt is the owner, not a rival.
        assert _lock("acquire", lock, a.pid).returncode == 0
        # A rival's release must not drop the owner's lock.
        _lock("release", lock, b.pid)
        assert lock.is_file()
        assert _lock("release", lock, a.pid).returncode == 0
        assert not lock.exists()
    finally:
        for p in (a, b):
            p.kill()


def test_a_lock_left_by_a_closed_window_is_taken_over(tmp_path):
    lock = tmp_path / ".setup" / "quickstart.lock"
    a, b = _sleeper(), _sleeper()
    try:
        assert _lock("acquire", lock, a.pid).returncode == 0
        a.kill()
        a.wait(10)
        time.sleep(0.5)
        r = _lock("acquire", lock, b.pid)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert lock.read_text(encoding="ascii").startswith("%d|" % b.pid)
    finally:
        for p in (a, b):
            p.kill()


def test_a_reused_pid_does_not_keep_a_lock_alive(tmp_path):
    lock = tmp_path / "quickstart.lock"
    b = _sleeper()
    try:
        lock.write_text("%d|12345" % b.pid, encoding="ascii")   # right PID, wrong start time
        assert _lock("acquire", lock, b.pid).returncode == 0
    finally:
        b.kill()


# ---- D28 / D29: configure_env.ps1 ----------------------------------------------------------

_CE = None


def _ce_funcs():
    global _CE
    if _CE is None:
        _CE = H.ps_functions("scripts/configure_env.ps1", "Read-EnvLines", "Write-EnvFileAtomic")
    return _CE


def test_configure_env_round_trips_non_ascii_unchanged(tmp_path):
    env = tmp_path / ".env"
    original = ("# m365-copilot-companion-mcp \u2014 environment template\r\n"
                "# \u65e5\u672c\u8a9e\u306e\u30b3\u30e1\u30f3\u30c8\r\n"
                "MCP_API_KEY=abc\r\n")
    env.write_bytes(original.encode("utf-8"))
    drv = _ce_funcs() + "\n$l = @(Read-EnvLines %s)\nWrite-EnvFileAtomic %s ((($l) -join \"`r`n\") + \"`r`n\")\n" % (
        _ps_str(env), _ps_str(env))
    for _ in range(3):                           # the mojibake used to grow with every save
        r = H.run_ps(drv, tmp_path)
        assert r.returncode == 0, r.stderr
    assert env.read_bytes() == original.encode("utf-8")
    assert not list(tmp_path.glob(".env.tmp-*")), "a temporary file was left behind"


def test_configure_env_write_replaces_and_creates(tmp_path):
    env = tmp_path / ".env"
    drv = _ce_funcs() + "\nWrite-EnvFileAtomic %s \"A=1`r`n\"\n" % _ps_str(env)
    assert H.run_ps(drv, tmp_path).returncode == 0
    before = os.stat(env).st_ino
    drv = _ce_funcs() + "\nWrite-EnvFileAtomic %s \"B=2`r`n\"\n" % _ps_str(env)
    r = H.run_ps(drv, tmp_path)
    assert r.returncode == 0, r.stderr
    assert env.read_bytes() == b"B=2\r\n"            # no BOM, replaced whole
    # A NEW FILE ID IS THE EVIDENCE OF A SWAP: an in-place rewrite (WriteAllText on .env, which
    # truncates first) keeps the file's identity; a temp file renamed over it does not.
    assert os.stat(env).st_ino != before, ".env was rewritten in place, not swapped in"


def test_env_defaults_swaps_the_file_in_rather_than_rewriting_it(tmp_path):
    """scripts/win/env_defaults.ps1 runs on EVERY start_all -- the most frequent .env writer."""
    env = tmp_path / ".env"
    original = "# — 日本語\r\nMCP_API_KEY=k\r\n"
    env.write_bytes(original.encode("utf-8"))
    before = os.stat(env).st_ino
    ps1 = H.REPO / "scripts" / "win" / "env_defaults.ps1"
    drv = ". %s\nEnsure-EnvDefaults -Root %s\n" % (_ps_str(ps1), _ps_str(tmp_path))
    r = H.run_ps(drv, tmp_path)
    assert r.returncode == 0, r.stderr
    text = env.read_bytes().decode("utf-8")
    assert text.startswith(original) and "MCP_REVIEW_P2C=0" in text
    assert os.stat(env).st_ino != before, ".env was rewritten in place, not swapped in"
    assert not list(tmp_path.glob(".env.tmp-*"))


def test_configure_env_no_longer_writes_in_place():
    src = (H.REPO / "scripts" / "configure_env.ps1").read_text(encoding="utf-8-sig")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    body = code.split("function Write-EnvFileAtomic", 1)[0] + code.split("Write-EnvFileAtomic $EnvPath", 1)[1]
    assert "WriteAllText($EnvPath" not in body, "a direct, truncating write of .env is back"
    assert "Get-Content -LiteralPath $Path -Encoding UTF8" in code
