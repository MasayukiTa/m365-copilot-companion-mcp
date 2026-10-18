# -*- coding: utf-8 -*-
r"""start_all keeps the timings of its :8765/conv poll in the run record.

The poll (8 attempts x 2 s) decides whether "bridge: started but :8765/conv did not answer
within ~16s" is reported, and until now the only trace of it was that sentence: a bridge that
answered at 17 s and one that never came looked the same. Wait-BridgeConv records every
attempt (when, how long the probe took, whether it answered) plus the first success or the
give-up time, and New-StartAllRunRecord puts it on the run's start_all_runs.jsonl line as
`bridge_poll`. LOGGING ONLY -- the budget is unchanged and a test below pins it.

HOW. Same method as scripts/test_a_failed_start_says_what_and_where.py: the functions are cut
out of scripts/start_all.ps1 by balanced-brace extraction and run in a separate powershell. The
only listener is a loopback stub in this process. Windows-only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import threading
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

_FUNCS = ["Http-Up", "Wait-BridgeConv", "Get-BridgePollAttempts",
          "Get-Win32ProcessByPid", "ConvertFrom-WmiDate", "Get-LaunchLineage", "Get-StartAllMode",
          "New-StartAllRunRecord", "Write-StartAllRunRecord", "Save-StartAllRunLines",
          "Save-StartAllRunLinesMany", "Enter-StartAllRunLogLock"]

def _source() -> str:
    with open(START_ALL, encoding="utf-8") as fh:
        return fh.read()

def _extract_braced_block(text: str, start_marker: str) -> str:
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

def _driver(functions, root, body):
    pre = "\n".join([
        '$ErrorActionPreference = "Continue"',
        "$root = %s" % _q(root),
        "$script:startupFailures = @()",
        "$script:phaseTimings = [ordered]@{}",
        '$script:lastFailure = ""',
        '$script:lastFailureStage = ""',
        "$script:launch = @{}",
        "$script:busyHolderPid = 0",
        "$script:bridgePoll = $null",
        "$script:runStartedAt = Get-Date",
        'function Hide-Secrets([string]$text) { return $text }',
    ])
    return pre + "\n\n" + functions + "\n\n" + body + "\n"

def _run(tmp_path, text):
    p = tmp_path / ("drv_%s.ps1" % uuid.uuid4().hex[:8])
    p.write_text('[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n' + text,
                 encoding="utf-8-sig")
    r = childproc.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                      timeout=240)
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    raise AssertionError("no RESULT line.\nstdout:\n%s\nstderr:\n%s"
                         % (r.stdout[-3000:], r.stderr[-3000:]))

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

class _SlowToStart(BaseHTTPRequestHandler):
    """Answers 503 to the first `refuse` probes and 200 after: a bridge whose server is up
    but not yet ready, deterministic however slow the probing powershell is."""
    seen = 0
    refuse = 2

    def do_GET(self):
        type(self).seen += 1
        code = 503 if type(self).seen <= type(self).refuse else 200
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass

BODY = r"""
$poll = Wait-BridgeConv %s %d %d
$script:bridgePoll = $poll
$runs = Join-Path $root "runs.jsonl"
$null = Write-StartAllRunRecord $runs (New-StartAllRunRecord "ok")
"RESULT:" + (@{ up = $poll.up } | ConvertTo-Json -Compress)
"""

def _records(tmp_path):
    return [json.loads(l) for l in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()]

def test_a_late_answer_is_recorded_with_its_first_success_time(tmp_path):
    port = _free_port()
    _SlowToStart.seen, _SlowToStart.refuse = 0, 2
    srv = ThreadingHTTPServer(("127.0.0.1", port), _SlowToStart)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = BODY % (_q("http://127.0.0.1:%d/conv" % port), 8, 1)
        r = _run(tmp_path, _driver(functions_text(), str(tmp_path), body))
    finally:
        srv.shutdown()
        srv.server_close()
    assert r["up"] is True, r
    rec = _records(tmp_path)[-1]
    bp = rec["bridge_poll"]
    assert bp["up"] is True and bp["gave_up_s"] is None, bp
    attempts = bp["attempts"]
    assert [a["ok"] for a in attempts] == [False, False, True], bp
    assert bp["first_ok_s"] >= 3.0, "three attempts, one second apart: %r" % bp
    assert all({"n", "at_s", "took_s", "ok"} <= set(a) for a in attempts), (
        "the attempts did not survive the record's JSON depth: %r" % attempts)

def test_giving_up_records_every_attempt_and_the_give_up_time(tmp_path):
    body = BODY % (_q("http://127.0.0.1:%d/conv" % _free_port()), 3, 1)
    r = _run(tmp_path, _driver(functions_text(), str(tmp_path), body))
    assert r["up"] is False, r
    bp = _records(tmp_path)[-1]["bridge_poll"]
    assert bp["up"] is False and bp["first_ok_s"] is None, bp
    assert len(bp["attempts"]) == 3 and not any(a["ok"] for a in bp["attempts"]), bp
    assert bp["gave_up_s"] >= 3.0 and bp["budget_s"] == 3, bp

def test_a_run_without_a_bridge_poll_records_null(tmp_path):
    body = r"""
$runs = Join-Path $root "runs.jsonl"
$null = Write-StartAllRunRecord $runs (New-StartAllRunRecord "ok")
"RESULT:" + (@{ ok = $true } | ConvertTo-Json -Compress)
"""
    _run(tmp_path, _driver(functions_text(), str(tmp_path), body))
    assert _records(tmp_path)[-1]["bridge_poll"] is None

def test_the_budget_is_unchanged_and_the_call_site_uses_the_recorder():
    """LOGGING ONLY: 8 attempts x 2 s still, and the old inline loop is gone."""
    src = _source()
    assert re.search(r"function Get-BridgePollAttempts\(\[string\]\$ImplAgentUrl, \[int\]\$MaxAttempts = 8\)",
                     src)
    assert 'Wait-BridgeConv "http://127.0.0.1:8765/conv" $bridgePollAttempts 2' in src
    assert "did not answer within ~16s" in src

_CACHE = []

def functions_text():
    if not _CACHE:
        src = _source()
        out = []
        for name in _FUNCS:
            m = re.search(r"(?m)^function %s\b" % re.escape(name), src)
            assert m, "start_all.ps1 no longer defines %s" % name
            out.append(_extract_braced_block(src, m.group(0)))
        _CACHE.append("\n\n".join(out))
    return _CACHE[0]
