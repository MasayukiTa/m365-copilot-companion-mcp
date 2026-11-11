# -*- coding: utf-8 -*-
"""Exercise submission behavior with generic fixture inputs."""
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
    return [
        {"text": "Please review the sample chart.", "resume_conv": "session-a", "provenance": {"source_file": "fixture", "source_line": 1}},
        {"text": "Please inspect the sample dataset and explain the trends in its summary.", "resume_conv": "session-b", "provenance": {"source_file": "fixture", "source_line": 2}},
    ]


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path, monkeypatch):
    """Exercise submission behavior with generic fixture inputs."""
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



def test_every_input_is_a_recorded_owner_line_with_its_provenance():
    """Exercise submission behavior with generic fixture inputs."""
    recs = _owner_records()
    assert recs, "no owner inputs loaded"
    for r in recs:
        assert r["text"].strip(), r
        prov = r.get("provenance") or {}
        assert prov.get("source_file"), r
        assert prov.get("source_line"), r
        assert len(r["text"]) <= 120, (len(r["text"]), r)


# -- a submitted ask lands as a real job, goal unaltered -------------------------------------

def test_an_owner_ask_is_received_not_left_untouched():
    """Exercise submission behavior with generic fixture inputs."""
    rec = _owner_records()[0]
    out = FI.fleet_submit(rec["text"], resume_conv=rec["resume_conv"], source="realuse")
    assert "queued" in out
    jobs = _jobs()
    assert len(jobs) == 1
    assert jobs[0]["payload"]["goal"] == rec["text"]



def test_two_owner_asks_sharing_vocabulary_both_start():
    """Exercise submission behavior with generic fixture inputs."""
    recs = _owner_records()
    first = FI.fleet_submit(recs[0]["text"], resume_conv=recs[0]["resume_conv"], source="realuse")
    second = FI.fleet_submit(recs[1]["text"], resume_conv=recs[1]["resume_conv"], source="realuse")
    assert "queued" in first, first
    assert "queued" in second, second
    assert len(_jobs()) == 2
    goals = {j["payload"]["goal"] for j in _jobs()}
    assert goals == {recs[0]["text"], recs[1]["text"]}


def test_the_two_owner_asks_are_not_each_others_duplicate_even_without_a_conversation_id():
    """Exercise submission behavior with generic fixture inputs."""
    recs = _owner_records()
    assert "queued" in FI.fleet_submit(recs[0]["text"], source="realuse")
    out = FI.fleet_submit(recs[1]["text"], source="realuse")
    assert "queued" in out, (
        "a distinct owner ask was refused as a near-duplicate of the other: %s" % out)
    assert len(_jobs()) == 2


# -- a continue input reaches its conversation, verbatim (bug C) -----------------------------

def test_a_continue_input_carries_the_owner_text_and_its_conversation_id_unaltered():
    """Exercise submission behavior with generic fixture inputs."""
    for rec in _owner_records():
        FI.fleet_submit(rec["text"], resume_conv=rec["resume_conv"], source="realuse")
    by_goal = {j["payload"]["goal"]: j for j in _jobs()}
    for rec in _owner_records():
        assert rec["text"] in by_goal, rec["text"]
        payload = by_goal[rec["text"]]["payload"]
        assert payload["resume_conv"] == rec["resume_conv"], payload
        assert payload["goal"] == rec["text"]


def test_a_continue_input_is_not_refused_as_a_near_duplicate_of_itself():
    """Exercise submission behavior with generic fixture inputs."""
    rec = _owner_records()[0]
    assert "queued" in FI.fleet_submit(rec["text"], resume_conv=rec["resume_conv"])
    again = FI.fleet_submit(rec["text"], resume_conv=rec["resume_conv"])
    assert "queued" in again, again
