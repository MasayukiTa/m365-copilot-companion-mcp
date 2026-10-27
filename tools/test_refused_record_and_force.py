# -*- coding: utf-8 -*-
"""A refusal the owner can SEE and OVERRULE.

The owner's rule: the system must not silently drop a request it decided not to queue. Two
halves make that real, and this file pins both against FAKE job records in an isolated queue --
no live fleet, no real .fleet.

  1. REFUSAL LEAVES A RECORD. When fleet_submit turns a goal away (duplicate / too long /
     queue full / empty / loop), it writes .fleet/tasks/refused/<id>.json holding the WHOLE
     goal, the reason KIND, the look-alike job and its measured overlap, the time, and who
     sent it. The one-line jsonl digest (tools/obs_log) is a separate record and is left alone.
  2. FORCE SKIPS THE CHECKS. force_submit_refused(id) re-queues that exact goal with every
     admission check skipped, stamps the job 'forced by a user', carries the originating
     refusal id, and marks the refused record forced -- and it is NOT on main.py's TOOLS, so
     an agent reaching this machine over the tunnel cannot call it through call_tool. Only a
     person (the cockpit button, the operator CLI) does.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_PATH)

from relay import task_router as TR  # noqa: E402
from tools import fleet_intake as FI  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path, monkeypatch):
    """A queue of its own, the same isolation tools/test_fleet_intake.py uses: point TR.TASKS
    at a tmp dir so neither the refused records nor the forced jobs this file writes land in
    the real .fleet/tasks in front of whatever drains it. REFUSED_DIR rides TR.TASKS, so this
    one patch moves it too."""
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    TR.ensure_dirs()
    return tmp_path


def _refused_records():
    d = FI._refused_dir()
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


def _pending_jobs():
    d = os.path.join(TR.TASKS, "pending")
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


# -- 1. a refusal leaves a full record -------------------------------------------------------

def test_a_too_long_goal_leaves_a_refused_record_with_the_whole_goal():
    """The jsonl digest keeps only an 80-char head; the forced resubmission re-sends the WHOLE
    instruction, so the refused record must keep the goal in full."""
    goal = "xy " * 2000                       # well over MAX_GOAL_CHARS once whitespace-collapsed
    out = FI.fleet_submit(goal, source="phone")
    assert "over the" in out                   # it really was refused
    (rec,) = _refused_records()
    assert rec["reason"] == "too_long"
    assert rec["source"] == "phone"
    assert rec["forced"] is False
    # the WHOLE goal, not a head: normalised (whitespace-collapsed) but not truncated
    assert rec["goal"] == " ".join(goal.split())
    assert "id" in rec and rec["id"]
    assert rec["refused_at"] > 0


def test_a_near_duplicate_is_accepted_and_only_noted_not_refused(monkeypatch):
    """A near-duplicate is NO LONGER refused -- the owner's 2026-10-26/27 order (fix/no-content-
    based-skip): the fleet door does not turn a goal away on content resemblance. Even with the
    duplicate guard turned back on (fleet_submit_dup_guard=on), a look-alike is ACCEPTED: the
    goal is queued, the resemblance is only NOTED in the background (_log_similarity_observed,
    and the job is tagged possible_duplicate_of), and -- because there was no refusal -- NO
    refused record is written. There is therefore nothing here to force in later.

    This replaces the old 'a duplicate refusal records the look-alike and the overlap' case,
    which asserted the opposite (guard on => refuse => a 'similar' refused record). That path
    was removed on purpose; keeping both intents is impossible, so the branch's accept-and-note
    behaviour wins and this test pins it. See <name>_merge_out/explain.md."""
    noted = []
    monkeypatch.setattr(FI, "_dup_guard_on", lambda: True)
    monkeypatch.setattr(FI, "_duplicate_of", lambda text: ("job123abc", 0.875))
    monkeypatch.setattr(FI, "_log_similarity_observed",
                        lambda text, source, similar_to="", similarity=0.0:
                        noted.append((similar_to, similarity)))
    out = FI.fleet_submit("do the same big thing again please now", source="x")
    # accepted, not refused: the reply is a normal queued-ok line, not a refusal
    assert "queued" in out
    assert "refused" not in out
    # the resemblance was noted in the background, naming the look-alike and the overlap
    assert noted == [("job123abc", 0.875)]
    # and the queued job carries the tag, so a reader can still see the resemblance
    (job,) = _pending_jobs()
    assert job["payload"]["possible_duplicate_of"] == "job123abc"
    # no refusal happened, so there is no refused record to force later
    assert _refused_records() == []


def test_an_empty_goal_still_leaves_a_record():
    out = FI.fleet_submit("   ", source="x")
    assert out.startswith("[fleet_submit: refused -- an empty goal")
    (rec,) = _refused_records()
    assert rec["reason"] == "missing_goal"


def test_a_pending_full_refusal_leaves_a_record(monkeypatch):
    monkeypatch.setattr(FI, "_pending_count", lambda: FI.MAX_PENDING)
    out = FI.fleet_submit("a brand new distinct goal here", source="x")
    assert "already waiting" in out
    (rec,) = _refused_records()
    assert rec["reason"] == "pending_full"


def test_an_accepted_goal_leaves_no_refused_record():
    """The record is for REFUSALS only -- a goal that queues must not also leave one."""
    out = FI.fleet_submit("list last month's mail with subject and sender", source="x")
    assert "queued" in out
    assert _refused_records() == []


def test_an_address_in_source_is_redacted_in_the_record():
    FI.fleet_submit("   ", source="from someone@example.com via phone")
    (rec,) = _refused_records()
    assert "someone@example.com" not in rec["source"]


def test_a_refusal_whose_record_cannot_be_written_still_refuses(monkeypatch):
    """Never raises: a refusal whose record could not be written still refuses, it just cannot
    be forced later."""
    monkeypatch.setattr(FI, "_write_refused_record",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk on fire")))
    with pytest.raises(OSError):
        FI._write_refused_record("missing_goal", "", "x")   # the patched one does raise
    # but the real path swallows it: monkeypatch a raising os.makedirs instead
    monkeypatch.undo()
    monkeypatch.setattr(FI.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    out = FI.fleet_submit("   ", source="x")
    assert out == "[fleet_submit: refused -- an empty goal is not an instruction]"


# -- 2. force skips the checks and marks both sides ------------------------------------------

def _make_refused(reason="similar", goal="force this exact goal through now",
                  source="operator", similar_to="job999", similarity=0.9):
    """Write a FAKE refused record by the real writer and return its id."""
    rid = FI._write_refused_record(reason, goal, source,
                                   similar_to=similar_to, similarity=similarity)
    assert rid
    return rid, goal


def test_force_submit_queues_the_goal_skipping_the_duplicate_check(monkeypatch):
    """The override exists precisely to get past a refusal -- so it must queue even when the
    duplicate/capacity checks that fleet_submit runs would still say no."""
    # Make every admission check that fleet_submit runs REFUSE, to prove force bypasses them.
    monkeypatch.setattr(FI, "_duplicate_of", lambda text: ("job999", 0.99))
    monkeypatch.setattr(FI, "_pending_count", lambda: FI.MAX_PENDING)
    rid, goal = _make_refused()

    jid = FI.force_submit_refused(rid, source="cockpit")
    assert jid and not jid.startswith("[")       # a bare job id, not a bracketed error

    (job,) = _pending_jobs()
    assert job["id"] == jid
    assert job["payload"]["goal"] == goal        # the whole goal, unchanged


def test_the_forced_job_carries_the_user_mark_and_the_refusal_id():
    rid, _goal = _make_refused()
    jid = FI.force_submit_refused(rid, source="cockpit")
    (job,) = _pending_jobs()
    # the 'forced by a user' mark, on the payload beside the goal...
    assert job["payload"]["forced_by_user"] is True
    assert job["payload"]["refused_id"] == rid
    # ...and on the origin, where a reader that walks provenance sees it
    assert job["origin"]["forced_by_user"] is True
    assert job["origin"]["via"] == "operator_force"
    assert job["origin"]["refused_id"] == rid


def test_forcing_stamps_the_refused_record_forced_in():
    rid, _goal = _make_refused()
    jid = FI.force_submit_refused(rid, source="cli")
    (rec,) = _refused_records()
    assert rec["forced"] is True
    assert rec["forced_jid"] == jid
    assert rec["forced_at"] > 0


def test_forcing_the_same_refusal_twice_does_not_double_submit():
    """A second press of the button (or a second CLI run) must not queue the goal again."""
    rid, _goal = _make_refused()
    jid = FI.force_submit_refused(rid, source="cli")
    again = FI.force_submit_refused(rid, source="cli")
    assert again.startswith("[force_submit_refused:")
    assert "already forced" in again
    assert len(_pending_jobs()) == 1


def test_forcing_an_unknown_id_is_a_bracketed_message_not_a_crash():
    out = FI.force_submit_refused("nosuchid", source="cli")
    assert out.startswith("[force_submit_refused: no refused record")
    assert _pending_jobs() == []


def test_a_crafted_id_cannot_walk_out_of_the_refused_directory():
    """The id shape is validated, so a path-traversal value reads as 'no such record'."""
    assert FI._read_refused_record("../../etc/passwd") is None
    out = FI.force_submit_refused("../../x", source="cli")
    assert out.startswith("[force_submit_refused: no refused record")


# -- 3. the override is NOT on the agent-facing tool surface ---------------------------------

def test_force_submit_refused_is_not_an_mcp_tool():
    """An agent reaching this machine over the tunnel fetches main.py's TOOLS and can call
    whatever is there through call_tool. force_submit_refused must not be among them, so the
    'skip the checks' capability is reachable only by a person at the machine.

    READ FROM SOURCE, NOT BY IMPORTING main. Importing main runs its module body, which calls
    require_api_key() and ends the process (SystemExit 78) on a machine with no bearer token --
    exactly the CI/Linux checkout. The property is about what the TOOLS tuple NAMES, so the
    source of that tuple is what is read: force_submit_refused must not appear in it, nor be
    pulled in by the fleet_intake import line beside fleet_submit/fleet_queue.
    """
    import ast

    main_src = open(os.path.join(REPO_PATH, "main.py"), encoding="utf-8").read()
    tree = ast.parse(main_src)

    # 1) the fleet_intake import line brings in only the agent-facing names
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tools.fleet_intake":
            imported = {a.name for a in node.names}
            assert imported == {"fleet_submit", "fleet_queue"}, (
                "the fleet_intake import line now pulls in %r -- force_submit_refused and the "
                "CLI must stay off the module surface main builds TOOLS from" % imported)

    # 2) the override is never NAMED anywhere in main -- not in TOOLS, not in _ESSENTIAL, not
    #    in any pin list. A tool the agent can reach has to be referenced by name somewhere main
    #    builds its surface from; force_submit_refused and its CLI entry appear nowhere, while
    #    the agent-facing fleet_submit/fleet_queue do. (Comments are not Name nodes, so the
    #    reasoning paragraphs about the override above do not count as a reference.)
    referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "force_submit_refused" not in referenced, (
        "force_submit_refused is referenced in main.py -- it must stay off the agent tool surface")
    assert "fleet_submit" in referenced
    assert "fleet_queue" in referenced


def test_the_operator_cli_reaches_force_but_import_does_not_touch_argv():
    """The CLI branch is guarded by __main__, so importing tools.fleet_intake never parses a
    command line -- and the CLI wires --force-refused to force_submit_refused."""
    import inspect
    src = inspect.getsource(FI._cli)
    assert "--force-refused" in src
    assert "force_submit_refused" in src
