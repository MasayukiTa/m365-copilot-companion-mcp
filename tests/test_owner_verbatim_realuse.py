# -*- coding: utf-8 -*-
"""Real use, in the owner's own words -- not a goal someone wrote for the test.

Every input here is a sentence the OWNER actually typed, lifted verbatim from the
recorded submissions and carried in tests/realuse_owner_inputs/owner_verbatim_submissions.jsonl
with its provenance (which file it came from, which line, which conversation it
continued). Nothing is paraphrased, and no scaffolding sentence a handler added
("work only in this clone", "do not write to H:") is used as an input -- those are
the handler's words, not the owner's, and the owner's standing instruction is that
only what the owner typed may be a test input.

The two owner lines are both CONTINUATIONS: each was sent to an existing Copilot
conversation (its resume_conv/sess id travels with it) and each was dropped when the
conversation could not be resumed. They are the live data for three properties the
owner asked to be confirmed:

  * two near-in-topic asks in a row both start -- the duplicate guard does not fold
    one owner ask into the other (bug A: shared wording must not cause a false
    duplicate refusal);
  * a submitted ask does not sit queued-but-untouched -- it lands as a real pending
    job whose goal is the owner's text, unaltered;
  * a "continue" input reaches the original conversation verbatim -- the resume_conv
    id rides on the payload and the goal text is byte-for-byte the owner's (bug C).

What is NOT here, and why: the completed-card ordering and the post-clear scroll
(bugs E and D) cannot be decided by this hermetic path -- they are read off the live
Fleet Cockpit window with the screen tools. They are left to the screen-driven,
by-hand check described in realuse_tests_out/explain.md, not faked with a synthetic
assertion that would pass without ever touching the window.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import task_router as TR  # noqa: E402
from tools import fleet_intake as FI  # noqa: E402

_INPUTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "realuse_owner_inputs", "owner_verbatim_submissions.jsonl")


def _owner_records():
    """The owner's verbatim lines, read from the committed input file -- never inlined
    here, so the test and the recorded provenance can never drift apart."""
    out = []
    with open(_INPUTS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path, monkeypatch):
    """A queue of its own, exactly as tools/test_fleet_intake.py does it: writing into
    the real .fleet/tasks would put the owner's words in front of whatever drains it."""
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    TR.ensure_dirs()
    return tmp_path


def _jobs():
    d = os.path.join(TR.TASKS, "pending")
    out = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


# -- the input file is the owner's words, unaltered ------------------------------------------

def test_every_input_is_a_recorded_owner_line_with_its_provenance():
    """The guard against the thing the owner forbade: a test input that someone wrote.
    Each record must carry where the line came from, so a reader can check it against
    the recording rather than trust the test."""
    recs = _owner_records()
    assert recs, "no owner inputs loaded"
    for r in recs:
        assert r["text"].strip(), r
        prov = r.get("provenance") or {}
        assert prov.get("source_file"), r
        assert prov.get("source_line"), r
        # The owner's rule: around 100 characters, no handler scaffolding. A line that
        # carried the clone/branch/"do not write to H:" preamble would be the handler's.
        assert "\u4f5c\u696d\u7528\u306e\u8907\u88fd" not in r["text"], r  # 作業用の複製
        assert "SharePoint" not in r["text"], r
        assert len(r["text"]) <= 120, (len(r["text"]), r)


# -- a submitted ask lands as a real job, goal unaltered -------------------------------------

def test_an_owner_ask_is_received_not_left_untouched():
    """'received from the door' means a pending job whose goal is the owner's text, not a
    reply that says queued while nothing was written."""
    rec = _owner_records()[0]
    out = FI.fleet_submit(rec["text"], resume_conv=rec["resume_conv"], source="realuse")
    assert "queued" in out
    jobs = _jobs()
    assert len(jobs) == 1
    assert jobs[0]["payload"]["goal"] == rec["text"]


# -- two near-in-topic owner asks in a row both start (bug A) --------------------------------

def test_two_owner_asks_sharing_vocabulary_both_start():
    """Both owner lines are about the same investigation and share content words
    (\u30c7\u30fc\u30bf, \u89e3\u6790). As continuations they carry a resume_conv, so each must be admitted
    on its own conversation id -- the duplicate guard must not read the second as a
    resubmission of the first and refuse it."""
    recs = _owner_records()
    first = FI.fleet_submit(recs[0]["text"], resume_conv=recs[0]["resume_conv"], source="realuse")
    second = FI.fleet_submit(recs[1]["text"], resume_conv=recs[1]["resume_conv"], source="realuse")
    assert "queued" in first, first
    assert "queued" in second, second
    assert len(_jobs()) == 2
    goals = {j["payload"]["goal"] for j in _jobs()}
    assert goals == {recs[0]["text"], recs[1]["text"]}


def test_the_two_owner_asks_are_not_each_others_duplicate_even_without_a_conversation_id():
    """Stronger form of the same property, with the resume_conv exemption removed: these
    are genuinely different owner asks, so even submitted as fresh goals the overlap
    guard must not fold the second into the first. If this ever starts refusing, the
    guard is folding two distinct owner instructions together -- the real bug A."""
    recs = _owner_records()
    assert "queued" in FI.fleet_submit(recs[0]["text"], source="realuse")
    out = FI.fleet_submit(recs[1]["text"], source="realuse")
    assert "queued" in out, (
        "a distinct owner ask was refused as a near-duplicate of the other: %s" % out)
    assert len(_jobs()) == 2


# -- a continue input reaches its conversation, verbatim (bug C) -----------------------------

def test_a_continue_input_carries_the_owner_text_and_its_conversation_id_unaltered():
    """The owner's 'continue' line must reach the ORIGINAL conversation as itself: the
    resume_conv id rides on the payload (so the runner threads it to that conversation
    instead of guessing a new one by goal text), and the goal is byte-for-byte what the
    owner typed. This is the exact drop bug C is about -- a continuation that became a
    new, context-free task."""
    for rec in _owner_records():
        FI.fleet_submit(rec["text"], resume_conv=rec["resume_conv"], source="realuse")
    by_goal = {j["payload"]["goal"]: j for j in _jobs()}
    for rec in _owner_records():
        assert rec["text"] in by_goal, rec["text"]
        payload = by_goal[rec["text"]]["payload"]
        assert payload["resume_conv"] == rec["resume_conv"], payload
        # verbatim: the stored goal equals the owner's line with nothing added or cut
        assert payload["goal"] == rec["text"]


def test_a_continue_input_is_not_refused_as_a_near_duplicate_of_itself():
    """A follow-up to a conversation is deliberately terse and reads like the resubmission
    the duplicate guard refuses; carrying resume_conv it must still go through. Submitting
    the same owner continuation twice (as a drop-and-resend would) must not have the
    resend refused for overlap."""
    rec = _owner_records()[0]
    assert "queued" in FI.fleet_submit(rec["text"], resume_conv=rec["resume_conv"])
    again = FI.fleet_submit(rec["text"], resume_conv=rec["resume_conv"])
    assert "queued" in again, again
