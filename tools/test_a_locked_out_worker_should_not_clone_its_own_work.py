# -*- coding: utf-8 -*-
"""A worker that cannot get past a gate must not answer by handing the same work to a second one.

WHAT HAPPENED, 2026-09-15 17:44-18:42. A fleet worker could not obtain an unlock token for a
PowerPoint rebuild and, instead of stopping, called `fleet_submit` to delegate the same job
onward (`origin.source` recorded, verbatim: "delegated:PPTX_v3_rebuild (unlock unavailable on
this client"). Thirty-two minutes later a SECOND submission for the same rebuild arrived from a
worker as well. A THIRD arrived from the operator's side at 18:42 -- a deliberate resubmission of
a run that had been stopped, which is not the failure this file is about and must keep working.

None of the three were byte-identical: one was a worker's own paraphrase, one carried extra
context, one was the operator's original wording. `fleet_submit` refused an empty goal, an
over-long goal and a full queue already -- each a shape check on the ONE goal arriving -- but
nothing compared an arriving goal against what this machine already had queued or running, so an
authorisation failure anywhere upstream turned into copies of the work rather than a visible stop.
Each copy occupies a worker, a Copilot conversation and a share of the machine, and each one can
repeat the same move.

WHY WORD OVERLAP, NOT EXACT TEXT. tools/skill_lessons.py grouped this same shape of problem
(two freely-worded attempts at one job) by Jaccard overlap of content words, specifically BECAUSE
its predecessor -- exact-match after path/date/number normalisation -- returned zero pairs where
a real one had been added: normalising a path to `<path>` and then requiring equal strings put
"do X" and "do X, files are at C:\\..." in different groups. That is the exact defect an
exact-match refusal here would have: none of the three real submissions shared enough literal
text to be caught by one, but they shared the vocabulary of the same job.

WHAT THIS FILE PINS. A paraphrase of a goal already running is refused, and the refusal names
the running job. A goal that only shares a topic, not the work, is accepted. A goal whose
original has actually finished is accepted -- a deliberate resubmission of stopped work must
never be mistaken for a duplicate. And relay/fanout.py's near-identical-by-construction sibling
goals are shown never to reach this door at all, so the dedup rule cannot be the thing that
strangles a legitimate split.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fanout  # noqa: E402
from relay import task_router as TR  # noqa: E402
from tools import fleet_intake as FI  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """A queue AND a fleet-state directory of its own.

    tools/test_fleet_intake.py's own fixture only had to isolate TR.TASKS, because nothing in
    fleet_intake.py read .fleet/status.json before this file's change. Now that it does,
    leaving FLEET_STATE_DIR unpatched here would make every test in THIS file read whatever
    real status.json this machine's live fleet happens to be writing at the moment the suite
    runs -- conftest.py already redirects it globally to a per-process temp directory, which
    keeps a bare `import` safe, but a test that wants to PLANT a worker record needs its own
    directory, not one shared with every other test in the same pytest process.
    """
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    TR.ensure_dirs()
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path / "state"))
    os.makedirs(os.path.join(str(tmp_path / "state")), exist_ok=True)
    return tmp_path


def _write_status(tmp_path, workers):
    with open(os.path.join(str(tmp_path / "state"), "status.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"running": True, "workers": workers}, fh, ensure_ascii=False)


def _running_worker(jid, goal):
    return {"jid": jid, "goal": goal, "status": "ready", "closed": False, "outcome": None}


def _finished_worker(jid, goal, outcome="DONE"):
    return {"jid": jid, "goal": goal, "status": outcome.lower(), "closed": True,
            "outcome": outcome}


def _jobs():
    d = os.path.join(TR.TASKS, "pending")
    out = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


# The goal the measured incident was about, paraphrased -- not the real product/file names,
# which do not belong in a tracked file (see MEMORY.md on this repo's history rewrite).
_ORIGINAL = (
    "Rebuild the activity-report slide deck to match the written spec and save it as a new "
    "file. Input is the 28-page deck with three embedded videos at "
    "C:/work/deck/report_v2.pptx -- do not modify it. Output the finished 27-page version "
    "as report_v3.pptx in the same folder. Read the rebuild spec first and follow every "
    "section literally: fixed rules, template shapes, page order, notes, self-check."
)

# A worker's own paraphrase of the same job -- reworded, reordered, some detail dropped --
# which is what actually arrived the second time in the measured incident.
_PARAPHRASE = (
    "User asked (via a worker's own paraphrase of a slide-deck redo task). Spec: follow the "
    "rebuild spec at C:/work/deck literally, section by section -- fixed rules, template "
    "shapes, page order, notes, self-check. Produce report_v3.pptx as a new 27-page file; "
    "the original 28-page report_v2.pptx with its three embedded videos must not be touched."
)

# Genuinely different work that happens to mention slides and a similar folder -- must not be
# caught by a rule that is only supposed to catch the SAME job reworded.
_DIFFERENT = (
    "Open the quarterly budget workbook at C:/work/finance/q3_budget.xlsx and add a pivot "
    "table summarising spend by department on a new sheet named Summary. Do not touch the "
    "raw data tab."
)


def test_a_paraphrase_of_a_running_workers_goal_is_refused_and_names_it(isolated_state):
    _write_status(isolated_state, [_running_worker("run1", _ORIGINAL)])
    out = FI.fleet_submit(_PARAPHRASE)
    assert "refused" in out and "run1" in out, out
    assert _jobs() == [], "a refused goal must not be queued"


def test_a_paraphrase_of_a_pending_goal_is_also_refused(isolated_state):
    """Not only status.json: a goal still sitting in this door's own pending/ queue, never
    having reached a worker yet, is exactly as much "already in flight" as one being worked."""
    FI.fleet_submit(_ORIGINAL)
    first_id = _jobs()[0]["id"]
    out = FI.fleet_submit(_PARAPHRASE)
    assert "refused" in out and first_id in out, out
    assert len(_jobs()) == 1


def test_a_genuinely_different_goal_is_accepted_even_with_something_in_flight(isolated_state):
    _write_status(isolated_state, [_running_worker("run1", _ORIGINAL)])
    out = FI.fleet_submit(_DIFFERENT)
    assert "queued" in out, out
    assert len(_jobs()) == 1


def test_a_goal_whose_original_worker_has_finished_is_accepted(isolated_state):
    """The operator's real, deliberate move in the measured incident: resubmitting a stopped
    run. A finished worker (`closed`, a terminal `status`/`outcome`) must not count as
    in-flight, or every legitimate retry of a genuinely stalled job would be refused forever."""
    _write_status(isolated_state, [_finished_worker("run1", _ORIGINAL, outcome="STUCK")])
    out = FI.fleet_submit(_PARAPHRASE)
    assert "queued" in out, out


def test_a_goal_no_longer_in_any_live_queue_directory_is_accepted(isolated_state):
    """The router moves a dispatched job out of pending/ entirely -- see relay/task_router.py's
    `done/` note ("done/ DOES NOT MEAN THE WORK IS DONE... the job's own status says what
    actually happened"). Once nothing in pending/, running/, for_fleet/, awaiting_ack/ or a
    live worker still names the goal, a resubmission is not a duplicate of anything this
    machine can see -- which is the correct, if imperfect, answer once the record itself has
    stopped carrying the text (see this task's report on what .fleet/tasks/done/*.json
    actually retains)."""
    out = FI.fleet_submit(_PARAPHRASE)
    assert "queued" in out, out


def test_a_short_goal_is_never_compared_ambiguously(isolated_state):
    """Below DUP_MIN_WORDS content words, overlap is not evidence either way -- this file's own
    sibling suite (tools/test_fleet_intake.py) submits bare goals like "do a thing" dozens of
    times across independent tests; the dedup rule must not treat any of them as duplicates of
    each other or of a real in-flight goal that merely shares one short word."""
    _write_status(isolated_state, [_running_worker("run1", _ORIGINAL)])
    out = FI.fleet_submit("do a thing")
    assert "queued" in out, out


def test_fanout_children_are_built_by_relay_fleet_directly_not_through_this_door():
    """relay/fanout.py's `child_goals` builds each sibling as the WHOLE parent goal plus a
    slice header ("range i/n") and a per-slice step -- near-identical by construction, which is
    exactly the shape this dedup rule must not strangle. It cannot, because fanout never calls
    fleet_submit: `add_box` (relay/relay_fleet.py) is an in-process list the running fleet loop
    appends straight-up Worker spawns to, entirely inside that one process, and never touches
    tools/fleet_intake.py or .fleet/tasks/pending at all. This test pins that boundary rather
    than exercising the dedup rule against synthetic fanout goals, which would be testing a path
    that cannot be reached."""
    import inspect

    src = inspect.getsource(fanout)
    assert "fleet_submit" not in src and "fleet_intake" not in src

    # And the converse, so this test would fail loudly the day someone wires fanout through
    # the intake door instead of leaving a false sense of safety behind.
    steps = ["handle the 1-10 range", "handle the 11-20 range"]
    kids = fanout.child_goals("do the full report", steps, campaign_id="c1")
    assert len(kids) == 2
    # The two siblings ARE near-identical by construction -- sharing the whole parent goal --
    # which is precisely why they must never be pushed through fleet_submit's dedup path.
    from tools.fleet_intake import _dup_similarity, _dup_words
    sim = _dup_similarity(_dup_words(kids[0]["text"]), _dup_words(kids[1]["text"]))
    assert sim > FI.DUP_SIMILARITY, (
        "fanout siblings are expected to be well ABOVE the dedup threshold by construction "
        "(%.2f) -- which is exactly why they must never reach fleet_submit" % sim)
