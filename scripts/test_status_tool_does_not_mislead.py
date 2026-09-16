# -*- coding: utf-8 -*-
"""The status tool's own rules, pinned -- because it exists to stop the machine misleading us.

Every rule below is a mistake made on 2026-09-16, most of them by the tool's author within
minutes of writing the tool that was supposed to prevent them.

  A PARSER THAT DEGRADES TO "?" IS A SIGNAL THAT LIES QUIETLY. The first run printed "?" for
  every process start time, because ConvertTo-Json renders a CIM datetime as "/Date(ms)/"
  rather than the WMI string the parser expected. Nothing said it had failed; it looked like
  a machine whose processes had no start times.

  IDLE IS NOT UNKNOWN. A fleet with nothing to do writes nothing, and the first run marked
  that "??" -- conflating "correct and quiet" with "I cannot tell", which is the exact
  conflation the tool was written against.

  A CHECK MUST NAME THE RIGHT REASON. "no run on record to compare against" was printed when
  a run WAS on record and merely carried no floor. A wrong reason sends the next reader to
  the wrong place, which costs more than saying nothing.

  AND IT MUST NEVER COUNT ITSELF. Querying processes by command line matches the querying
  process. That turned an idle machine into an apparent crash-loop for twenty minutes.
"""
from __future__ import annotations

import os
import re
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import status as S  # noqa: E402


# --------------------------------------------------------------------- start times
def test_a_start_time_is_read_in_every_shape_powershell_emits():
    """ConvertTo-Json gives /Date(ms)/; raw WMI gives yyyymmddhhmmss; some hosts localise."""
    epoch_ms = 1789550349000
    expected = time.strftime("%m-%d %H:%M:%S", time.localtime(epoch_ms / 1000.0))
    assert S.started_at({"CreationDate": "/Date(%d)/" % epoch_ms}) == expected
    assert S.started_at({"CreationDate": {"value": "/Date(%d)/" % epoch_ms}}) == expected
    assert S.started_at({"CreationDate": "20260916191909.123456+540"}) == "09-16 19:19:09"
    assert S.started_at({"CreationDate": "2026-09-16T19:19:09"}) == "09-16 19:19:09"


def test_an_unreadable_start_time_says_so_rather_than_printing_a_question_mark():
    """The first version returned "?" for everything and looked like missing data."""
    said = S.started_at({"CreationDate": "not a date at all"})
    assert "unreadable" in said
    assert said != "?"


# --------------------------------------------------------------------- self-exclusion
def test_the_process_list_never_contains_this_process():
    procs = S.processes()
    if procs is None:
        pytest.skip("process list unavailable on this host")
    assert all(p.get("ProcessId") != os.getpid() for p in procs)


def test_the_finder_ignores_the_query_that_produced_the_list():
    """The PowerShell that enumerates processes has every search term in its own command
    line. On 2026-09-16 that produced 'the supervisor is crash-looping every 12 seconds'."""
    planted = [{"ProcessId": 1, "CommandLine": "powershell ... ConvertTo-Json -Depth 2"},
               {"ProcessId": 2, "CommandLine": "python -m relay.fleet_runner --goals-file x"}]
    hits = S.find(planted, "relay.fleet_runner")
    assert [h["ProcessId"] for h in hits] == [2]


def test_an_unreadable_process_list_is_unknown_not_empty():
    assert S.find(None, "anything") is None


# --------------------------------------------------------------------- idle vs unknown
def _rows(section_fn, *args):
    rep = S.Report()
    section_fn(rep, *args)
    return [r for sec in rep.sections for r in sec["rows"]]


def test_an_idle_fleet_reads_as_a_known_state(tmp_path, monkeypatch):
    fleet = tmp_path / ".fleet"
    fleet.mkdir()
    (fleet / "status.json").write_text(
        '{"running": false, "done_count": 1, "total": 1, "updated": %d, "workers": []}'
        % int(time.time() - 7000), encoding="utf-8")
    monkeypatch.setattr(S, "REPO", str(tmp_path))
    run = [r for r in _rows(S.section_fleet) if r["name"] == "run"]
    assert run and run[0]["verdict"] == S.OK, run
    assert "idle" in run[0]["detail"]


def test_a_run_claiming_to_be_alive_while_silent_is_a_fault(tmp_path, monkeypatch):
    """The one case where a stale timestamp IS a problem, and it must not be softened."""
    fleet = tmp_path / ".fleet"
    fleet.mkdir()
    (fleet / "status.json").write_text(
        '{"running": true, "done_count": 0, "total": 1, "updated": %d, "workers": []}'
        % int(time.time() - 7000), encoding="utf-8")
    monkeypatch.setattr(S, "REPO", str(tmp_path))
    run = [r for r in _rows(S.section_fleet) if r["name"] == "run"]
    assert run and run[0]["verdict"] == S.BAD, run
    assert "wedged" in run[0]["detail"]


# --------------------------------------------------------------------- the cross-check
def _crosscheck_rows(settings_keys, run):
    rep = S.Report()
    settings = {"keys": settings_keys} if settings_keys is not None else None
    S._crosscheck_floor(rep, settings, run)
    return [r for sec in rep.sections for r in sec["rows"]]


def test_a_disagreement_between_the_panel_and_the_run_is_reported_as_a_fault():
    """THE 2026-09-16 DEFECT. settings.txt said 1 for a month; every run used 4."""
    rows = _crosscheck_rows({"disk_floor_gb": "1"}, {"disk_floor_gb": 4.0})
    assert rows[0]["verdict"] == S.BAD
    assert "disagree" in rows[0]["detail"]
    assert "OTHER context" in rows[0]["detail"], "it must say how to find the cause"


def test_agreement_is_reported_plainly():
    rows = _crosscheck_rows({"disk_floor_gb": "1"}, {"disk_floor_gb": 1.0})
    assert rows[0]["verdict"] == S.OK


def test_no_floor_in_the_run_names_that_reason_and_not_another():
    """It used to say "no run on record" when a run was on record and carried no floor."""
    rows = _crosscheck_rows({"disk_floor_gb": "1"}, {"running": False})
    assert rows[0]["verdict"] == S.OK
    assert "no run is holding a floor" in rows[0]["detail"]
    assert "no .fleet/status.json" not in rows[0]["detail"]


def test_a_missing_status_file_is_unknown():
    rows = _crosscheck_rows({"disk_floor_gb": "1"}, None)
    assert rows[0]["verdict"] == S.UNK
    assert "status.json" in rows[0]["detail"]


# --------------------------------------------------------------------- it must not leak
def test_the_tool_never_prints_the_tunnel_name():
    """MCP_TUNNEL_NAME is this repository's own name, which must not reach a tracked file --
    and this tool's output is pasted into issues and commit messages."""
    src = open(os.path.join(REPO, "scripts", "status.py"), encoding="utf-8").read()
    assert "MCP_TUNNEL_NAME" not in src.split('"""', 2)[2], \
        "the tunnel name key is referenced outside the module docstring"
    # The tunnel section prints an ORIGIN derived from the URL, never the name.
    assert re.search(r'origin\s*=\s*"/"\.join\(url\.split\("/"\)\[:3\]\)', src)
