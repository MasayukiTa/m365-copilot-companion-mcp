# -*- coding: utf-8 -*-
r"""A startup that fails must still say WHAT failed and in WHICH stage -- and a startup whose
server is not answering must not exit 0 with "Done." on screen.

Two defects, one file:

  1. THE CAUSE IS NOWHERE. $script:startupFailures carried the problems to the screen, to
     .setup\logs\start_all_summary.txt and, as a COUNT, to start_all_runs.jsonl. A count is not a
     cause: a run that ended "3 problem(s)" hours ago -- or on a PC nobody was watching -- left
     nothing that said which stage failed and why, so the only way to find out was to make it
     happen again. start_all now writes the failure text AND the stage it happened in to ONE
     small file, .setup\last_failure.txt, OVERWRITTEN by every failure and DELETED by a run with
     nothing to report; the same two values ride on the run's start_all_runs.jsonl line.

  2. A RUN THAT EXITED 0 OVER A DEAD SERVER. Every stage could pass and http://127.0.0.1:8000/
     health still answer nothing: the supervisor process existed, the tunnel was hosted, the run
     printed "Done." and returned 0, and Copilot Studio was the thing that found out. The
     endpoint is now asked at the END of a run (retried for 30 s) and a server that does not
     answer is a FAILURE -- the exit code is the failure count, so that run can no longer be
     mistaken for a good one.

HOW. Same method as scripts/test_start_all_install_path.py: the functions are cut out of
scripts/start_all.ps1 by balanced-brace extraction and run in a separate powershell against a
temp "checkout". Nothing of the live stack is touched: no supervisor, bridge, Edge, tunnel or
MCP server is started, and the only listener this file creates is a loopback HTTP stub on a free
port in the test process -- which is exactly what the health probe has to find, and to fail to
find when it does not answer 200.

Windows-only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

START_ALL = os.path.join(HERE, "start_all.ps1")
_POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")

pytestmark = pytest.mark.skipif(os.name != "nt" or not _POWERSHELL,
                                reason="start_all.ps1 is Windows PowerShell only")

#: The functions the drivers below need. Extracted by balanced-brace block, exactly as
#: scripts/test_start_all_install_path.py does -- this file never runs start_all.ps1.
_FUNCS = ["Env-Value",
          "Resolve-DevTunnelExe", "Invoke-DevTunnelBounded", "Get-TunnelLoginState",
          "Get-TunnelHostCount", "Test-TunnelServing",
          "Test-McpServerHealth", "Wait-McpServerHealth",
          "Set-StartupStage", "Get-StartupStage", "Get-StartAllFailurePath",
          "Write-StartupFailureFile", "Remove-StartAllFailureFile", "Save-StartupFailure",
          "Get-Win32ProcessByPid", "ConvertFrom-WmiDate", "Get-LaunchLineage", "Get-StartAllMode",
          "New-StartAllRunRecord", "Write-StartAllRunRecord", "Save-StartAllRunLines",
          "Save-StartAllRunLinesMany", "Enter-StartAllRunLogLock"]


def _source() -> str:
    return open(START_ALL, encoding="utf-8").read()


def _extract_braced_block(text: str, start_marker: str) -> str:
    """The balanced-brace block starting at `start_marker` (not string-aware; every brace inside
    a string in the extracted functions is itself balanced)."""
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


def _q(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


@pytest.fixture(scope="module")
def functions() -> str:
    src = _source()
    out = []
    for name in _FUNCS:
        m = re.search(r"(?m)^function %s\b" % re.escape(name), src)
        assert m, "start_all.ps1 no longer defines %s" % name
        out.append(_extract_braced_block(src, m.group(0)))
    return "\n\n".join(out)


@pytest.fixture()
def checkout(tmp_path):
    root = tmp_path / "co"
    (root / ".setup").mkdir(parents=True)
    (root / "scripts").mkdir()
    return root


def _driver(functions, root, body):
    pre = "\n".join([
        '$ErrorActionPreference = "Continue"',
        "$root = %s" % _q(root),
        "$scriptDir = %s" % _q(os.path.join(root, "scripts")),
        "$script:startupFailures = @()",
        "$script:startupUnclear = @()",
        "$script:phaseTimings = [ordered]@{}",
        '$script:startupStage = "start"',
        '$script:lastFailure = ""',
        '$script:lastFailureStage = ""',
        "$script:launch = @{}",
        "$script:busyHolderPid = 0",
        "$script:runStartedAt = Get-Date",
        'function Hide-Secrets([string]$text) { return $text }',
    ])
    return pre + "\n\n" + functions + "\n\n" + body + "\n"


def _ps(tmp_path, text, env=None, timeout=240):
    p = tmp_path / ("drv_%s.ps1" % uuid.uuid4().hex[:8])
    # Forced, as scripts/test_start_all_install_path.py forces it: the raw bytes this powershell
    # writes to a redirected stdout pipe are governed by [Console]::OutputEncoding, not by
    # $OutputEncoding, and the runner's OS code page may not carry the failure text.
    text = '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n' + text
    p.write_text(text, encoding="utf-8-sig")
    return childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                         env=env, timeout=timeout)


def _result(r):
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    raise AssertionError("no RESULT line.\nstdout:\n%s\nstderr:\n%s" % (r.stdout[-3000:], r.stderr[-3000:]))


def _run(tmp_path, functions, checkout, body, env=None):
    return _result(_ps(tmp_path, _driver(functions, checkout, body), env=env))


def _failure_file(checkout):
    return checkout / ".setup" / "last_failure.txt"


def _fields(path):
    """The small file, as a dict, exactly the way a person reading it would take it."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _s, v = line.partition("=")
            out[k] = v
    return out


def _refused_url():
    """A port nothing is listening on: connection refused, found and released at once."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d/health" % port, port


# ==================================================== claim 1: what failed, and where

def test_a_failure_is_written_down_with_the_stage_it_happened_in(tmp_path, checkout, functions):
    """The whole point of the file: a reader who comes back later gets the failure TEXT and the
    STAGE, not a count. The stage is the one Set-StartupStage named when the stage started, so a
    failure is attributed to the stage that caused it."""
    body = r"""
Set-StartupStage "deps_sync"
$script:startupFailures += "Python dependencies could not be launched: a stub reason -- re-run start_all.bat"
$null = Save-StartupFailure
"RESULT:" + (@{ stage = $script:lastFailureStage; text = $script:lastFailure; n = @($script:startupFailures).Count } | ConvertTo-Json -Compress)
"""
    r = _run(tmp_path, functions, checkout, body)
    assert r["stage"] == "deps_sync", r
    assert "a stub reason" in r["text"], r
    f = _failure_file(checkout)
    assert f.is_file(), "no .setup\\last_failure.txt was written"
    fields = _fields(f)
    assert fields["stage"] == "deps_sync", fields
    assert "a stub reason" in fields["failure"], fields
    assert fields["failures"] == "1", fields
    assert "re-run start_all.bat" in f.read_text(encoding="utf-8"), "the file must say what to do"


def test_the_file_is_overwritten_on_every_failure_never_appended_to(tmp_path, checkout, functions):
    """Overwritten, so it describes the LAST failure of a run rather than growing a history: a
    stale cause is worse than no cause. And no .tmp is left behind (temp-then-rename)."""
    body = r"""
Set-StartupStage "deps_sync"
$script:startupFailures += "the first failure"
$null = Save-StartupFailure
Set-StartupStage "tunnel_confirm"
$script:startupFailures += "the second failure"
$null = Save-StartupFailure
"RESULT:" + (@{ n = @($script:startupFailures).Count } | ConvertTo-Json -Compress)
"""
    r = _run(tmp_path, functions, checkout, body)
    assert r["n"] == 2, r
    text = _failure_file(checkout).read_text(encoding="utf-8")
    assert "the second failure" in text and "stage=tunnel_confirm" in text, text
    assert "the first failure" not in text, "the file accumulated failures instead of being rewritten"
    assert len(text.splitlines()) == 4, text
    assert not list((checkout / ".setup").glob("*.tmp")), "a .tmp was left behind"


def test_a_run_with_nothing_to_report_deletes_the_previous_cause(tmp_path, checkout, functions):
    """Deleted, so the file cannot outlive the fault it described -- the next run that fails
    overwrites it and the run after a fix removes it."""
    f = _failure_file(checkout)
    f.write_text("stage=deps_sync\r\nfailures=1\r\nfailure=an old, fixed fault\r\n", encoding="utf-8")
    body = r"""
$gone = Remove-StartAllFailureFile (Get-StartAllFailurePath $root)
$again = Remove-StartAllFailureFile (Get-StartAllFailurePath $root)
"RESULT:" + (@{ gone = $gone; again = $again } | ConvertTo-Json -Compress)
"""
    r = _run(tmp_path, functions, checkout, body)
    assert r == {"gone": True, "again": True}, r
    assert not f.exists(), "a clean run left the previous failure behind"


def test_a_stage_that_fails_names_its_own_stage(tmp_path, checkout, functions):
    """Driven through a real stage function, not through Set-StartupStage directly: a signed-out
    devtunnel recorded by Test-TunnelServing must land in the file as stage=tunnel_confirm. A
    stub devtunnel.cmd stands in for the CLI (read-only verbs only), exactly as
    scripts/test_start_all_install_path.py drives the same function."""
    bin_dir, lad = tmp_path / "bin", tmp_path / "localappdata"
    bin_dir.mkdir()
    lad.mkdir()
    (bin_dir / "devtunnel.cmd").write_text("\r\n".join([
        "@echo off",
        'if "%1"=="user" echo Not logged in.',
        'exit /b 0',
        ""]), encoding="ascii")
    (checkout / ".env").write_text("MCP_TUNNEL_NAME=t1\r\n", encoding="ascii")
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    env = dict(os.environ, LOCALAPPDATA=str(lad), PATH=";".join([str(bin_dir), sysroot,
                                                                 os.path.join(sysroot, "System32")]))
    body = r"""
Test-TunnelServing -HostWaitSec 2 -PollSec 1
"RESULT:" + (@{ failures = @($script:startupFailures) } | ConvertTo-Json -Compress)
"""
    r = _run(tmp_path, functions, checkout, body, env=env)
    assert len(r["failures"]) == 1 and "signed out" in r["failures"][0], r
    fields = _fields(_failure_file(checkout))
    assert fields["stage"] == "tunnel_confirm", fields
    assert "signed out" in fields["failure"], fields


def test_the_run_record_carries_the_failure_and_its_stage(tmp_path, checkout, functions):
    """The jsonl line used to say only "failures": 2. A reader looking for a cause hours later
    gets the text and the stage there too -- and empty strings, not nulls or a missing key, on a
    run that had nothing to report (so a reader can index into every line)."""
    runs = checkout / ".setup" / "logs" / "start_all_runs.jsonl"
    (checkout / ".setup" / "logs").mkdir(parents=True, exist_ok=True)
    body = r"""
Set-StartupStage "browser_bridge_ui"
$script:startupFailures += "bridge: a stub port is held by pid 4321"
$null = Save-StartupFailure
$ok = Write-StartAllRunRecord %s (New-StartAllRunRecord "failures")
"RESULT:" + (@{ ok = $ok } | ConvertTo-Json -Compress)
""" % _q(runs)
    r = _run(tmp_path, functions, checkout, body)
    assert r["ok"] is True, r
    rec = json.loads(runs.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["failure_stage"] == "browser_bridge_ui", rec
    assert "pid 4321" in rec["failure"], rec
    assert rec["failures"] == 1, rec

    # A run that recorded nothing -- driven in its own process, the way a second start_all would
    # see it -- still writes the two keys, as empty strings.
    clean_body = r"""
$c = New-StartAllRunRecord "ok"
"RESULT:" + (@{ n = @($script:startupFailures).Count; stage = $c.failure_stage; text = $c.failure } | ConvertTo-Json -Compress)
"""
    c = _run(tmp_path, functions, checkout, clean_body)
    assert c == {"n": 0, "stage": "", "text": ""}, c


def test_the_failure_file_is_the_one_small_file_at_the_repo_root():
    """Named, not guessed: .setup\\last_failure.txt, next to .setup\\convenience_provisioned, and
    built in ONE place so the writer, the deleter and the tests cannot drift apart."""
    src = _source()
    assert 'Join-Path $RepoRoot ".setup\\last_failure.txt"' in src, \
        "the failure file is not .setup\\last_failure.txt"
    assert "(Get-StartAllFailurePath $root)" in src, "the path helper is not what callers use"


def test_every_counted_failure_is_also_written_down():
    """A ratchet, because the failure list is built by a += line in ~20 places and any ONE of
    them that forgot to record itself would recreate exactly the defect this file is about: a
    counted failure with no cause on disk."""
    lines = _source().splitlines()
    appends = 0
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") or "$script:startupFailures +=" not in line:
            continue
        appends += 1
        if "Save-StartupFailure" in line.split("+=", 1)[1]:
            continue                                    # the two one-line catch handlers
        following = next((l for l in lines[i + 1:] if l.strip()), "")
        assert "Save-StartupFailure" in following, (
            "line %d appends a failure but nothing writes it down:\n  %s\n  %s"
            % (i + 1, line.strip(), following.strip()))
    assert appends >= 15, "only %d failure appends were found; the ratchet stopped matching" % appends


# ==================================================== claim 2: the server is asked last

def _health_stub(fail_first=0):
    """A loopback /health: 404 for the first `fail_first` requests, then 200. Owned by THIS test
    process on a free port; nothing of the product is started."""
    state = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            state["n"] += 1
            body, status = (b"ok", 200) if (self.path == "/health" and state["n"] > fail_first) \
                else (b"", 404)
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/health" % srv.server_address[1]


def test_health_means_200_from_health_and_nothing_else(tmp_path, checkout, functions):
    """The one answer that means the server is up AND serving. A closed port is not it, and a
    listener that answers something else is not it either -- the question is not "is the port
    open" but "does /health answer 200", which is what the cockpit and Copilot Studio ask."""
    srv, url = _health_stub()
    dead, _port = _refused_url()
    try:
        body = r"""
$u = %s
$d = %s
$r = @{
  up     = (Test-McpServerHealth $u);
  wrong  = (Test-McpServerHealth ($u -replace '/health', '/nothing-here'));
  dead   = (Test-McpServerHealth $d);
  waited = (Wait-McpServerHealth $d 0 1);
}
"RESULT:" + ($r | ConvertTo-Json -Compress)
""" % (_q(url), _q(dead))
        r = _run(tmp_path, functions, checkout, body)
    finally:
        srv.shutdown()
        srv.server_close()
    assert r == {"up": True, "wrong": False, "dead": False, "waited": False}, r


def test_the_health_check_retries_until_the_server_answers(tmp_path, checkout, functions):
    """Retried, because the supervisor may have started the server seconds ago and it imports
    before it binds (the reason quickstart waits for this endpoint before measuring it). A probe
    that asked once would report a failure on a server that was merely still starting."""
    srv, url = _health_stub(fail_first=2)
    try:
        body = r"""
$t0 = Get-Date
$ok = Wait-McpServerHealth %s 10 1
"RESULT:" + (@{ ok = $ok; seconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1) } | ConvertTo-Json -Compress)
""" % _q(url)
        r = _run(tmp_path, functions, checkout, body)
    finally:
        srv.shutdown()
        srv.server_close()
    assert r["ok"] is True, r
    assert r["seconds"] >= 1, "the probe answered without retrying: %r" % r


def test_a_server_that_never_answers_gives_up_at_the_deadline(tmp_path, checkout, functions):
    """Bounded: the retry loop must END and report "no", or the launcher would hang instead of
    failing. The 30 s the tail asks for is passed in by the caller -- see the wiring test."""
    dead, _port = _refused_url()
    body = r"""
$t0 = Get-Date
$ok = Wait-McpServerHealth %s 2 1
"RESULT:" + (@{ ok = $ok; seconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1) } | ConvertTo-Json -Compress)
""" % _q(dead)
    r = _run(tmp_path, functions, checkout, body)
    assert r["ok"] is False, r
    assert 2 <= r["seconds"] < 30, "the retry loop did not stop at its own deadline: %r" % r


def test_the_health_check_is_the_last_thing_checked_and_can_flip_the_exit_code():
    """WHERE it sits is the fix: after every stage above (including the tunnel and sign-in
    checks), before the failure list is printed and written -- so a server that never answered is
    in the summary, in the run record, and in the exit code, which IS the failure count."""
    src = _source()
    signin = src.index('Record-PhaseTiming "m365_signin" $t0_m365Signin')
    health = src.index("Wait-McpServerHealth)")
    report = src.index("# WHAT WENT WRONG, SAID OUT LOUD AT THE END")
    summary = src.index("$summaryWritten = Write-StartupSummary")
    exit_code = src.index("exit $script:startupFailures.Count")
    assert signin < health < report < summary < exit_code, "the health check is not on the last leg"
    # Asked only when there is something to ask about: nothing was started on a lock timeout, and
    # a supervisor that died at once is already a counted failure (the tunnel check's own rule).
    block = src[src.rindex("if (-not $script:lockTimedOut)", 0, health):report]
    assert "Wait-McpServerHealth" in block
    assert "$script:supervisorDied" in block
    # The endpoint, and the retry the task asks for.
    assert 'function Wait-McpServerHealth([string]$Url = "http://127.0.0.1:8000/health", [int]$TimeoutSec = 30' in src
    # Reported, not printed: the failure joins the list the exit code counts, and the message
    # tells the operator the one thing they can do (the product rule: quickstart.bat /
    # start_all.bat are the only entry points a person runs; never "edit this file").
    assert "the MCP server did not answer http://127.0.0.1:8000/health within 30 s -- re-run start_all.bat" in src
    health_block = src[health:report]
    assert "startupFailures +=" in health_block and "Save-StartupFailure" in health_block, \
        "the health failure is printed but never counted or written down"


def test_a_run_with_no_failures_deletes_the_file_at_the_end_of_the_run():
    """The other half of "overwritten on each failure": a run that recorded nothing removes it,
    and does so while the run is still deciding its outcome (before the summary and the record),
    so what is on disk and what was recorded cannot disagree."""
    src = _source()
    delete = src.index("Remove-StartAllFailureFile (Get-StartAllFailurePath $root)")
    guard = src.rindex("if (@($script:startupFailures).Count -eq 0)", 0, delete)
    runs = src.rindex("$null = Write-StartAllRunRecord (Join-Path $script:diagDir")
    assert guard < delete, "the delete is not guarded by 'no failures were recorded'"
    assert runs > delete, "the file is removed only after the run record is written"
    assert src.index("exit $script:startupFailures.Count") > delete
