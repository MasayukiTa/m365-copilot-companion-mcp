# -*- coding: utf-8 -*-
"""A back-filled conversation id must be THIS transcript's, not the newest one with that goal.

MEASURED 2026-09-12, on the dry run that preceded any write -- which is why it was a dry run.
The first version of the resolver reused `socket_route.conversation_for_goal`'s rule, "newest
wins", and two different transcripts came back with the same id:

    r6aa4920b_a0_w0.jsonl -> 1a2c4e8d-89bf-4877-9876-cb1ea0f2a184
    r6aa492d6_a0_w0.jsonl -> 1a2c4e8d-89bf-4877-9876-cb1ea0f2a184

"Newest wins" is correct for the question conversation_for_goal asks -- a follow-up should
continue the LATEST run of a goal. It is wrong for the question a back-fill asks: which
conversation did THIS transcript run in. Listing the candidates showed the real answer:

    r6aa4920b  last line 1789170372   worker_done 08:46:12  gap    +0  conv d3710cc2...
                                      worker_done 09:06:33  gap +1221  conv 1a2c4e8d...
    r6aa492d6  last line 1789171511   worker_done 08:46:12  gap  -1139  conv d3710cc2...
                                      worker_done 09:06:33  gap   +82  conv 1a2c4e8d...

A WRONG ID IS WORSE THAN NONE. With none, the chat window says it cannot continue this
conversation. With a wrong one it opens a real conversation that is not this one, resumes it,
and looks correct doing it -- the same "plausible either way" failure the follow-up mechanism
exists to prevent.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import backfill_transcript_conv_ids as B  # noqa: E402

GOAL = "summarise the report"
#: The two real rows, keeping their measured 1221-second separation.
EARLY = (GOAL, "w0", 1789170372.0, "d3710cc2")
LATE = (GOAL, "w0", 1789171593.0, "1a2c4e8d")
ROWS = [EARLY, LATE]


# ── the defect ────────────────────────────────────────────────────────────────────────────

def test_the_earlier_transcript_keeps_its_own_conversation():
    """THE ONE THAT WOULD HAVE MISLABELLED A TRANSCRIPT. Newest-wins returned the 09:06 run's
    id for a transcript that stopped being written at 08:46."""
    assert B.resolve(ROWS, GOAL, "w0", 1789170372.0) == "d3710cc2"


def test_the_later_transcript_gets_the_later_conversation():
    assert B.resolve(ROWS, GOAL, "w0", 1789171511.0) == "1a2c4e8d"


def test_a_row_written_before_the_last_line_is_never_the_match():
    """worker_done is written when the worker finishes, so a row that predates the transcript's
    last line belongs to some other run by construction."""
    assert B.resolve([EARLY], GOAL, "w0", 1789171511.0) == "", (
        "a conversation that was already finished when this transcript was still being written "
        "was accepted as its own")


def test_a_distant_row_is_refused_rather_than_guessed():
    """Observed gaps on real data were 0, 82 and 116 seconds. Beyond the window there is no
    reason to believe the two belong together, and the back-fill's whole value is that the id
    can be trusted afterwards."""
    far = (GOAL, "w0", 1789170372.0 + B.MATCH_WINDOW_S + 1, "someone-else")
    assert B.resolve([far], GOAL, "w0", 1789170372.0) == ""


def test_another_worker_is_not_this_transcript():
    """A run can have twenty workers on one goal (the SWE arms do). Name is part of identity."""
    assert B.resolve([(GOAL, "w7", 1789170372.0, "w7-conv")], GOAL, "w0", 1789170372.0) == ""


def test_an_unnamed_row_can_still_match():
    """Older records may carry no worker name. Excluding them would lose real matches, and the
    timestamp still has to line up."""
    assert B.resolve([(GOAL, "", 1789170372.0, "c")], GOAL, "w0", 1789170372.0) == "c"


def test_a_different_goal_never_matches():
    assert B.resolve(ROWS, "a different goal", "w0", 1789170372.0) == ""


def test_nothing_to_go_on_answers_empty():
    assert B.resolve(ROWS, "", "w0", 1789170372.0) == ""
    assert B.resolve(ROWS, GOAL, "w0", 0) == ""
    assert B.resolve([], GOAL, "w0", 1789170372.0) == ""


# ── the write ─────────────────────────────────────────────────────────────────────────────

def _transcript(path, goal, name, lines):
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps({"meta": True, "key": "k", "name": name, "goal": goal},
                            ensure_ascii=False) + "\n")
        for i, (role, ts) in enumerate(lines):
            fh.write(json.dumps({"role": role, "text": "x", "turn": i + 1, "ts": ts},
                                ensure_ascii=False) + "\n")


def _state(tmp_path, rows):
    sd = tmp_path / "fleet"
    (sd / "transcripts").mkdir(parents=True)
    with io.open(str(sd / "socket_route.jsonl"), "w", encoding="utf-8", newline="") as fh:
        for goal, worker, ts, cid in rows:
            fh.write(json.dumps({"event": "worker_done", "goal": goal, "worker": worker,
                                 "ts": ts, "conv_client": cid}, ensure_ascii=False) + "\n")
    return sd


def test_the_line_lands_where_the_reader_looks(tmp_path):
    """_tx.note_guid writes the guid right after the first turn, and the chat window scans a
    bounded window of leading lines for it. A line appended at the end of a long transcript
    would be a line nothing reads."""
    sd = _state(tmp_path, ROWS)
    p = str(sd / "transcripts" / "r1_w0.jsonl")
    _transcript(p, GOAL, "w0", [("user", 1789170300.0), ("assistant", 1789170372.0)])

    B.backfill(str(sd), apply=True, out=lambda *a: None)

    lines = [json.loads(l) for l in io.open(p, encoding="utf-8") if l.strip()]
    assert lines[0].get("meta") is True, "the meta line must stay first"
    assert lines[1].get("guid") == "d3710cc2", "the guid is not where the reader looks"
    assert len(lines) == 4, "a turn was lost or duplicated"


def test_a_dry_run_writes_nothing(tmp_path):
    sd = _state(tmp_path, ROWS)
    p = str(sd / "transcripts" / "r1_w0.jsonl")
    _transcript(p, GOAL, "w0", [("assistant", 1789170372.0)])
    before = io.open(p, encoding="utf-8").read()

    tally = B.backfill(str(sd), apply=False, out=lambda *a: None)

    assert io.open(p, encoding="utf-8").read() == before
    assert tally["written"] == 0


def test_running_it_twice_does_not_write_twice(tmp_path):
    sd = _state(tmp_path, ROWS)
    p = str(sd / "transcripts" / "r1_w0.jsonl")
    _transcript(p, GOAL, "w0", [("assistant", 1789170372.0)])

    B.backfill(str(sd), apply=True, out=lambda *a: None)
    second = B.backfill(str(sd), apply=True, out=lambda *a: None)

    assert second["written"] == 0 and second["already"] == 1
    guids = [json.loads(l) for l in io.open(p, encoding="utf-8") if '"guid"' in l]
    assert len(guids) == 1


def test_a_transcript_it_cannot_place_is_left_alone(tmp_path):
    """Refusing is the point. The chat window already handles a conversation with no id -- it
    says so -- and that is strictly better than opening the wrong one."""
    sd = _state(tmp_path, [EARLY])
    p = str(sd / "transcripts" / "r1_w0.jsonl")
    _transcript(p, "an unrelated goal", "w0", [("assistant", 1789170372.0)])
    before = io.open(p, encoding="utf-8").read()

    tally = B.backfill(str(sd), apply=True, out=lambda *a: None)

    assert tally["written"] == 0 and tally["no_id"] == 1
    assert io.open(p, encoding="utf-8").read() == before


def test_a_live_worker_is_not_rewritten_under_it(tmp_path):
    """The write is a rewrite, not an append, so a worker still writing the file must be left
    alone until its run is over."""
    sd = _state(tmp_path, ROWS)
    p = str(sd / "transcripts" / "r1_w0.jsonl")
    _transcript(p, GOAL, "w0", [("assistant", 1789170372.0)])
    with io.open(str(sd / "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"running": True,
                   "workers": [{"name": "w0", "status": "waiting", "transcript": p}]}, fh)

    tally = B.backfill(str(sd), apply=True, out=lambda *a: None)

    assert tally["written"] == 0 and tally["skipped_live"] == 1
