# -*- coding: utf-8 -*-
r"""doctor.ps1: "MCP server up" FAIL must not call a planned restart a crash loop.

THE FALSE DIAGNOSIS (colleague's PC, 2026-10-20). Right after a git pull the supervisor cycles a
stale server on purpose ("server is running stale code and nothing is in flight -- cycling it"; the
exit is recorded "-- planned: stale code cycle"). For a few seconds the server is down by design.
doctor printed "the supervisor is relaunching the server and it is dying ... Do NOT run
start_all.bat: the supervisor is already doing that on a loop" from nothing but "server check
failed AND a supervisor exists".

THE RULE. Get-SupervisorServerVerdict reads the supervisor's own log:
  planned = within 90 s a planned exit or a launch, and no unplanned exit
  loop    = >= 3 launches in 10 min AND >= 2 exits that are not planned
  none    = anything else (the older branches then apply)
The function is extracted from the live doctor.ps1 and run in Windows PowerShell against stub logs
with a fixed "now". Nothing touches the real supervisor log, port 8000 or .fleet.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_setup_devtunnel_access_and_identity as base  # noqa: E402
from test_setup_devtunnel_access_and_identity import DOCTOR_PS1, _extract, _run_ps  # noqa: E402

pytestmark = base.pytestmark

NOW = "2026-10-20 09:34:00"

PLANNED_CYCLE = """\
2026-10-20 08:28:15  supervisor up
2026-10-20 08:28:19  MCP server process launched (stale instances cleared); output -> x
2026-10-20 08:40:00  tunnel login re-check ok
2026-10-20 09:33:43  server is running stale code and nothing is in flight -- cycling it so the checkout's fixes are live
2026-10-20 09:33:47  previous server pid=7960 exited code= () -- planned: stale code cycle
2026-10-20 09:33:50  MCP server process launched (stale instances cleared); output -> x
"""

CRASH_LOOP = "".join(
    "2026-10-20 09:%02d:00  server check failed (3/3) -- no answer\n"
    "2026-10-20 09:%02d:02  previous server pid=%d exited code=0xC0000005 (-1073741819) after 1m -- unplanned\n"
    "2026-10-20 09:%02d:05  MCP server process launched (stale instances cleared); output -> x\n"
    % (m, m, 100 + m, m) for m in (25, 27, 29, 31))

ONE_CRASH = """\
2026-10-20 09:33:30  server check failed (3/3) -- no answer
2026-10-20 09:33:32  previous server pid=11 exited code=0x1 (1) after 1m -- unplanned
2026-10-20 09:33:35  MCP server process launched (stale instances cleared); output -> x
"""

OLD_PLANNED = """\
2026-10-20 09:20:00  previous server pid=7960 exited code= () -- planned: stale code cycle
2026-10-20 09:20:03  MCP server process launched (stale instances cleared); output -> x
"""

SCENARIOS = {
    "planned": PLANNED_CYCLE,
    "loop": CRASH_LOOP,
    "one_crash": ONE_CRASH,
    "old_planned": OLD_PLANNED,
}


def _verdicts(tmp_path):
    src = open(DOCTOR_PS1, encoding="utf-8").read()
    fn = _extract(src, "function Get-SupervisorServerVerdict")
    body = fn + "\n$now = [datetime]::ParseExact('%s', 'yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)\n" % NOW
    for name, text in SCENARIOS.items():
        p = tmp_path / (name + ".log")
        p.write_text(text, encoding="ascii")
        body += ("$v = Get-SupervisorServerVerdict -Path '%s' -Now $now\n"
                 "Write-Output ('VERDICT|%s|' + $v.kind + '|' + (@($v.lines) -join '~'))\n" % (p, name))
    body += ("$v = Get-SupervisorServerVerdict -Path '%s' -Now $now\n"
             "Write-Output ('VERDICT|missing|' + $v.kind + '|')\n" % (tmp_path / "nope.log"))
    out = _run_ps(tmp_path, body)
    res = {}
    for l in out.splitlines():
        if l.startswith("VERDICT|"):
            _, name, kind, lines = l.split("|", 3)
            res[name] = (kind, lines)
    return res


def test_verdicts_from_stub_supervisor_logs(tmp_path):
    r = _verdicts(tmp_path)
    assert r["planned"][0] == "planned", r
    assert r["loop"][0] == "loop", r
    assert "unplanned" in r["loop"][1], r          # the last log lines are handed to the reader
    assert r["one_crash"][0] == "none", r          # a single unplanned exit is not a loop, nor planned
    assert r["old_planned"][0] == "none", r        # a planned cycle 14 minutes ago explains nothing now
    assert r["missing"][0] == "none", r


def test_one_advice_function_combines_listener_rule_and_supervisor_verdict(tmp_path):
    """ONE explanation: Get-ServerDownAdvice (listener rule of #218) takes the verdict as well."""
    src = open(DOCTOR_PS1, encoding="utf-8").read()
    assert src.count("$script:serverFix = ") == 1   # the single call below
    fn = _extract(src, "function Get-ServerDownAdvice")
    body = fn + "\n"
    cases = [
        ("planned_down", "$false", "@{ kind = 'planned'; lines = @() }"),
        ("planned_alive", "$true", "@{ kind = 'planned'; lines = @() }"),
        ("loop_down", "$false", "@{ kind = 'loop'; lines = @('L1','L2') }"),
        ("none_down", "$false", "$null"),
    ]
    for name, listening, verdict in cases:
        body += ("$t = Get-ServerDownAdvice -SupervisorRunning $true -Out $null -PyProblem '' "
                 "-Listening %s -Verdict %s\nWrite-Output ('ADV|%s|' + ($t -replace '\\s+', ' '))\n"
                 % (listening, verdict, name))
    out = _run_ps(tmp_path, body)
    adv = dict(l.split("|", 2)[1:] for l in out.splitlines() if l.startswith("ADV|"))
    assert "restarting the server after an update (planned)" in adv["planned_down"], adv
    assert "dying" not in adv["planned_down"].lower(), adv
    assert "IS listening on :8000" in adv["planned_alive"], adv     # the listener rule comes first
    assert "keeps dying" in adv["loop_down"] and "L1" in adv["loop_down"], adv
    assert "restarting the server after an update" not in adv["none_down"], adv


def test_doctor_stays_ascii_only():
    data = open(DOCTOR_PS1, "rb").read()
    assert all(b < 128 for b in data)
