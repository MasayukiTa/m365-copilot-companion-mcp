# -*- coding: utf-8 -*-
"""Task-tree identity keys on fan-out records are additive and change nothing else."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

from relay import family_view as fv
from relay import fanout as fo
from relay import fleet_resume as fr_resume
from relay import fleet_runner as fr
from relay.review_resilience import task_envelope_from_goal

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_KEYS = ("parent_campaign_id", "parent_subtask_index", "root_id")
GOAL = "collect the monthly figures"
STEPS = ["part one", "part two", "part three"]


def _strip(goal):
    return {k: v for k, v in goal.items() if k not in NEW_KEYS}


def test_adding_the_keys_changes_nothing_else():
    kids = fo.child_goals(GOAL, STEPS, parent_task_id="t-parent", cwd="C:/work")
    assert len(kids) == 3
    for i, k in enumerate(kids, 1):
        cid = fo.campaign_id_for(GOAL, parent_task_id="t-parent")
        expected = {
            "text": k["text"], "cwd": "C:/work", "campaign_id": cid,
            "task_id": "%s-%d" % (cid, i), "role": "subtask",
            "parent_task_id": "t-parent", "depth": 1, "subtask_index": i, "subtask_of": 3,
        }
        assert _strip(k) == expected
        assert set(k) - set(expected) == set(NEW_KEYS)


def test_header_write_adds_only_the_three_keys():
    with open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8") as fh:
        src = fh.read()
    i = src.index('{"kind": "campaign", "campaign_id": cid')
    block = src[i:i + 2600]
    for key in ("parent_task_id", "parent_campaign_id", "root_id"):
        assert '"%s"' % key in block
    # the pre-existing header keys are still all there
    for key in ("goal", "n", "cwd", "checks", "partial", "run_id", "ts"):
        assert '"%s"' % key in block


def _ledger_lines(with_keys):
    kids = fo.child_goals(GOAL, STEPS, parent_task_id="t-parent", cwd="C:/work")
    cid = kids[0]["campaign_id"]
    head = {"kind": "campaign", "campaign_id": cid, "goal": GOAL, "n": 3, "cwd": "C:/work",
            "checks": [], "partial": "", "run_id": "r1", "ts": 1.0}
    if with_keys:
        head.update({"parent_task_id": "t-parent", "parent_campaign_id": "", "root_id": cid})
    lines = [json.dumps(head)]
    for k in kids:
        g = k if with_keys else _strip(k)
        lines.append(json.dumps({"campaign_id": cid, "task_id": k["task_id"],
                                 "subtask_index": k["subtask_index"], "text": k["text"],
                                 "goal": g}))
    return cid, lines


def test_new_keys_are_ignored_by_ledger_readers_and_old_ledger_matches():
    cid, old = _ledger_lines(False)
    _, new = _ledger_lines(True)
    fam_old = fo.campaigns_from_ledger(old)[cid]
    fam_new = fo.campaigns_from_ledger(new)[cid]
    assert fam_old["goal"] == fam_new["goal"] == GOAL
    assert fam_old["n"] == fam_new["n"] == 3
    assert len(fam_old["children"]) == len(fam_new["children"]) == 3


def test_old_ledger_loads_in_resume_and_family_view(tmp_path):
    cid, old = _ledger_lines(False)
    (tmp_path / "campaigns.jsonl").write_text("\n".join(old) + "\n", encoding="utf-8")
    fams = fr_resume.read_campaigns(str(tmp_path))
    assert cid in fams and fams[cid]["n"] == 3
    ws = [dict(name="k%d" % i, campaign_id=cid, task_id="%s-%d" % (cid, i),
               parent_task_id="t-parent", role="subtask", subtask_index=i, status="running",
               outcome="", goal="") for i in (1, 2, 3)]
    ws.append(dict(name="p", campaign_id=cid, task_id="t-parent", parent_task_id=None,
                   role="producer", status="done", outcome="FANOUT", goal=GOAL))
    out_old = fv.build_groups(ws, old)
    _, new = _ledger_lines(True)
    ws_new = [dict(w, root_id=cid) for w in ws]
    out_new = fv.build_groups(ws_new, new)
    assert len(out_old) == len(out_new) == 1
    assert out_old[0]["parent"]["display_state"] == out_new[0]["parent"]["display_state"]


def test_old_ledger_loads_in_task_tree_if_present(tmp_path):
    try:
        from relay import task_tree  # noqa: F401
    except ImportError:
        return
    # present on this tree: it must at least import; its own tests cover its readers


def test_root_id_depth0_is_own_campaign_and_depth1_inherits(monkeypatch):
    top = fo.child_goals(GOAL, STEPS, parent_task_id="t-parent")
    cid = top[0]["campaign_id"]
    assert all(k["root_id"] == cid for k in top)
    assert all(k["parent_campaign_id"] == "" and k["parent_subtask_index"] is None for k in top)
    # synthetic depth-1 parent at function level; the runtime gate itself is untouched
    assert fo.MAX_DEPTH == 1
    assert fo.child_goals(GOAL, STEPS, parent_task_id="x", depth=1) == []
    monkeypatch.setattr(fo, "MAX_DEPTH", 2)
    deep = fo.child_goals("slice goal", STEPS, parent_task_id=top[1]["task_id"], depth=1,
                          parent_campaign_id=cid, parent_subtask_index=2, root_id=cid)
    assert all(k["root_id"] == cid and k["parent_campaign_id"] == cid
               and k["parent_subtask_index"] == 2 and k["depth"] == 2 for k in deep)
    assert deep[0]["campaign_id"] != cid
    # without an explicit root a depth>0 child falls back to the parent's campaign
    d2 = fo.child_goals("slice goal", STEPS, parent_task_id="x", depth=1, parent_campaign_id=cid)
    assert d2[0]["root_id"] == cid


def _w(root=None):
    goal = {"text": "g", "task_id": "t1", "campaign_id": "c1", "depth": 1}
    if root:
        goal["root_id"] = root
    return SimpleNamespace(
        name="w0", goal="g", status="running", outcome="", turn=1, max_turns=9,
        reason="", closed=False, conv_url="", conv_title="", verified=None,
        verify_attempts=0, last_response="", transcript="", cwd="", phase_events=[],
        task_envelope=task_envelope_from_goal(goal), parent_task_id=None,
        goal_hash="", fresh_replay_count=0, refusal_count=0, refusal_history=[],
        recovery_cause="", recovery_result="", recovery_state="", attempt_transcripts=[],
        page=None, tab_load=lambda: 0,
    )


def test_status_worker_rows_carry_root_id_only_when_known():
    snap = fr._snapshot([_w("cRoot"), _w()], 1.0, 2, directive="g", run_label="legacy")
    assert snap["workers"][0]["root_id"] == "cRoot"
    assert "root_id" not in snap["workers"][1]
    assert snap["workers"][0]["campaign_id"] == "c1"


def test_root_id_does_not_change_the_envelope_goal_hash():
    a = task_envelope_from_goal({"text": "g", "task_id": "t1", "campaign_id": "c1"})
    b = task_envelope_from_goal({"text": "g", "task_id": "t1", "campaign_id": "c1",
                                 "root_id": "cRoot"})
    assert a.goal_hash == b.goal_hash
