# -*- coding: utf-8 -*-
"""A resume re-queues campaign children of the INTERRUPTED RUN only.

Incident 2026-10-01: PR #86 G2 re-queued the unfinished children of every campaign in
.fleet/campaigns.jsonl that lacked `merge_done`. The real ledger held 63 old campaigns (none
merge_done, all written before the merge_done marker existed): resuming a 2-goal run queued 545 degraded goals, reset
last_run_done.json to {} and embedded 2.6 MB of campaign plans in the snapshot.

Hermetic: tmp_path state dirs, a synthetic ledger shaped like the real one (no real data).
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout, fleet_reaper, fleet_resume as fr  # noqa: E402

RUN = "r6abd9129_a0"
PARENT = "Write a report on the history of the bicycle, in three parts."


def _cid(text):
    return fanout.campaign_id_for(text)


def _header(goal, n, stamp=None, **kw):
    d = {"kind": "campaign", "campaign_id": _cid(goal), "goal": goal, "n": n, "cwd": "C:/w",
         "checks": [], "partial": ""}
    if stamp:
        d["run_id"] = stamp
        d["ts"] = 1790808400.0
    d.update(kw)
    return d


def _child(cid, i, full=True):
    text = "slice %d of %s" % (i, cid)
    row = {"campaign_id": cid, "task_id": "%s-%d" % (cid, i), "subtask_index": i, "text": text}
    if full:
        row["goal"] = {"text": text, "cwd": "C:/w", "campaign_id": cid, "role": "subtask",
                       "subtask_index": i, "subtask_of": 3, "depth": 1}
    return row


def _family(goal, n=3, stamp=None, full=True):
    cid = _cid(goal)
    return [_header(goal, n, stamp)] + [_child(cid, i, full) for i in range(1, n + 1)]


def _old_ledger(k=60, kids=9):
    """k legacy campaigns: no run stamp, degraded (text-only) children, none merge_done."""
    rows = []
    for j in range(k):
        rows += _family("old parent goal number %d " % j + "x" * 400, n=kids, full=False)
    return rows


def _write(tmp_path, rows):
    with io.open(str(tmp_path / "campaigns.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _snapshot(tmp_path, workers, run_id=RUN, **kw):
    d = tmp_path / "interrupted"
    d.mkdir(exist_ok=True)
    data = {"schema": 1, "run_id": run_id, "state": "pending", "written_ts": 100.0,
            "workers": workers, "resume": {"count": 0, "history": []}}
    data.update(kw)
    (d / (run_id + ".json")).write_text(json.dumps(data))


def _worker(goal, **kw):
    w = {"name": "w0", "status": "refuting", "run_id": RUN, "goal": goal, "role": "producer"}
    w.update(kw)
    return w


def _resume(tmp_path, done=None):
    logs = []
    goals, degraded = fr.resume_children_goals(str(tmp_path), done if done is not None else {},
                                               log=logs.append)
    return goals, degraded, logs


# ---- the incident shape ----------------------------------------------------------------

def test_sixty_old_campaigns_and_one_of_this_run_queue_only_this_runs_children(tmp_path):
    rows = _old_ledger(60)
    rows += _family(PARENT, n=3, stamp=RUN)
    _write(tmp_path, rows)
    _snapshot(tmp_path, [_worker(PARENT)])
    goals, degraded, _ = _resume(tmp_path)
    assert sorted(g["subtask_index"] for g in goals) == [1, 2, 3]
    assert {g["campaign_id"] for g in goals} == {_cid(PARENT)}
    assert degraded == 0


def test_done_children_of_this_run_are_still_skipped(tmp_path):
    # A child counts as finished when its answer is on the ledger too: DONE in the done-map
    # without any answer is recovered or re-queued once (tests/test_child_result_durable.py).
    _write(tmp_path, _old_ledger(5) + _family(PARENT, n=3, stamp=RUN)
           + [{"kind": "child_result", "campaign_id": _cid(PARENT), "subtask_index": 2,
               "outcome": "DONE", "result": "r"}])
    _snapshot(tmp_path, [_worker(PARENT)])
    done = {fr.goal_resume_key(_child(_cid(PARENT), 2)["goal"]): "DONE"}
    goals, _, _ = _resume(tmp_path, done)
    assert sorted(g["subtask_index"] for g in goals) == [1, 3]


def test_legacy_header_without_a_stamp_is_matched_by_the_parent_job(tmp_path):
    """No run id anywhere on the old lines: the snapshot's parent goal ties it to its family."""
    _write(tmp_path, _old_ledger(60) + _family(PARENT, n=3, stamp=None, full=False))
    _snapshot(tmp_path, [_worker(PARENT)])
    goals, degraded, _ = _resume(tmp_path)
    assert {g["campaign_id"] for g in goals} == {_cid(PARENT)}
    assert len(goals) == 3 and degraded == 3          # degraded but only THIS family's


def test_legacy_header_is_matched_by_a_worker_that_carries_the_campaign_id(tmp_path):
    other = "a goal whose text the snapshot no longer holds"
    _write(tmp_path, _old_ledger(10) + _family(other, n=2, full=False))
    _snapshot(tmp_path, [_worker("child", role="subtask", campaign_id=_cid(other))])
    goals, _, _ = _resume(tmp_path)
    assert {g["campaign_id"] for g in goals} == {_cid(other)}


def test_legacy_ledger_with_no_evidence_is_never_resumed(tmp_path):
    """Fail closed: no snapshot and no goals ledger -> nothing comes out of the campaigns."""
    _write(tmp_path, _old_ledger(60))
    assert _resume(tmp_path)[0] == []
    _snapshot(tmp_path, [_worker("an unrelated goal")])
    assert _resume(tmp_path)[0] == []


def test_a_header_stamped_by_another_run_is_not_taken_even_with_the_same_text(tmp_path):
    """campaign_id hashes the goal text: yesterday's identical goal has the same id."""
    _write(tmp_path, _family(PARENT, n=3, stamp="rOTHER_a0"))
    _snapshot(tmp_path, [_worker(PARENT)])
    assert _resume(tmp_path)[0] == []


def test_a_merged_campaign_of_this_run_queues_nothing(tmp_path):
    _write(tmp_path, _family(PARENT, n=3, stamp=RUN)
           + [{"kind": "merge_done", "campaign_id": _cid(PARENT)}])
    _snapshot(tmp_path, [_worker(PARENT)])
    assert _resume(tmp_path)[0] == []


def test_a_run_that_resumed_an_earlier_run_keeps_that_runs_families(tmp_path):
    """run 1 split; run 2 (a resume) died before re-stamping; run 3 resumes run 2."""
    _write(tmp_path, _old_ledger(20) + _family(PARENT, n=2, stamp="rRUN1_a0"))
    _snapshot(tmp_path, [_worker("x", run_id="rRUN1_a1")], run_id="rRUN1_a1",
              campaigns_scoped=True, campaigns=[{"campaign_id": _cid(PARENT)}],
              state="resumed")
    _snapshot(tmp_path, [_worker("y", run_id="rRUN2_a0")], run_id="rRUN2_a0",
              campaigns_scoped=False, resume={"count": 1, "lineage": "rRUN1_a1"}, written_ts=200.0)
    goals, _ = fr.resume_children_goals(str(tmp_path), {}, log=lambda m: None,
                                        scope=fr.interrupted_run_scope(str(tmp_path),
                                                                        "rRUN2_a0")[0])
    assert {g["campaign_id"] for g in goals} == {_cid(PARENT)}


def test_an_old_snapshot_that_embedded_the_whole_ledger_is_not_trusted(tmp_path):
    """Snapshots written before this fix list every campaign and carry no campaigns_scoped."""
    _write(tmp_path, _old_ledger(30))
    _snapshot(tmp_path, [_worker("unrelated")],
              campaigns=[{"campaign_id": c} for c in fr.read_campaigns(str(tmp_path))])
    assert _resume(tmp_path)[0] == []


def test_a_synthetic_copy_of_the_real_ledger_shape_queues_nothing_for_a_two_goal_run(tmp_path):
    """63 campaigns, ~9 degraded children each, 2.6 MB: the measured incident."""
    _write(tmp_path, _old_ledger(63, kids=9))
    (tmp_path / "last_run_goals.json").write_text(json.dumps(
        {"started": 1.0, "goals": [{"text": "Write three sentences about green tea."},
                                   {"text": "Write about the bicycle."}]}))
    _snapshot(tmp_path, [_worker("Write three sentences about green tea."),
                         _worker("Write about the bicycle.")])
    assert len(fr.read_campaigns(str(tmp_path))) == 63
    assert _resume(tmp_path)[0] == []


# ---- the cap ---------------------------------------------------------------------------

def test_the_cap_is_the_larger_of_twenty_and_four_times_the_runs_goals():
    assert fr.resume_queue_cap(0) == 20 and fr.resume_queue_cap(2) == 20
    assert fr.resume_queue_cap(5) == 20 and fr.resume_queue_cap(6) == 24
    assert fr.resume_queue_cap(None) == 20


def _run_main(tmp_path, monkeypatch, capsys, env_lineage=RUN):
    import relay.fleet_runner as fleet_runner
    monkeypatch.setattr(sys, "argv", ["fleet_runner", "--resume", "--state-dir", str(tmp_path),
                                      "--agent-url", "https://example.invalid/chat"])
    monkeypatch.setenv("MCP_FLEET_RESUME_LINEAGE", env_lineage)
    monkeypatch.setattr(fleet_runner, "_setup_coordinator_log", lambda *a, **k: None)
    return fleet_runner


def test_a_resume_that_would_queue_too_many_goals_is_refused_and_stays_pending(
        tmp_path, monkeypatch, capsys):
    """Force the membership rule to say 'everything' (a future mistake): the cap still stops it."""
    fleet_runner = _run_main(tmp_path, monkeypatch, capsys)
    _write(tmp_path, _old_ledger(30, kids=9))
    _snapshot(tmp_path, [_worker("x")])
    ledger_goals = [{"text": "g1"}, {"text": "g2"}]
    fleet_runner._write_goals_ledger(str(tmp_path), ledger_goals, time.time())
    done_before = {"jid:keep": "DONE"}
    (tmp_path / "last_run_done.json").write_text(json.dumps(done_before))
    monkeypatch.setattr(fr, "interrupted_run_scope",
                        lambda *a, **k: (set(fr.read_campaigns(str(tmp_path))), "forced"))
    rc = fleet_runner.main()
    out = capsys.readouterr().out
    assert rc == 6 and "REFUSING TO RESUME" in out and "cap is 20" in out
    snap = json.load(open(tmp_path / "interrupted" / (RUN + ".json")))
    assert snap["state"] == "pending"
    assert snap["resume"]["blocked"]["reason"] == fr.RESUME_CAP_REASON
    # nothing was written that a queue would need, and the done map is untouched
    assert json.load(open(tmp_path / "last_run_done.json")) == done_before
    assert not (tmp_path / "fleet_run_active.json").exists()


def test_record_resume_does_not_flip_a_cap_refused_snapshot_to_resumed(tmp_path):
    _snapshot(tmp_path, [])
    path = str(tmp_path / "interrupted" / (RUN + ".json"))
    assert fr.record_resume_refused(str(tmp_path), RUN, 5.0, 545, 20)
    assert fr.record_resume(path, 6.0, 1, "sig")          # the resumer runs after the launch
    data = json.load(open(path))
    assert data["state"] == "pending" and data["resume"]["count"] == 1
    assert data["resume"]["blocked"]["cap"] == 20


# ---- last_run_done.json ----------------------------------------------------------------

def test_a_resume_does_not_reset_the_done_map(tmp_path, monkeypatch, capsys):
    """Reach the point where a fresh run writes {} and prove a resume does not."""
    fleet_runner = _run_main(tmp_path, monkeypatch, capsys)
    _snapshot(tmp_path, [_worker("x")])
    fleet_runner._write_goals_ledger(str(tmp_path), [{"text": "unfinished goal"}], time.time())
    done = {"jid:abc": "DONE", "deadbeef00000000": "DONE"}
    (tmp_path / "last_run_done.json").write_text(json.dumps(done))

    class _Stop(Exception):
        pass

    def _boom(*a, **k):
        raise _Stop()
    monkeypatch.setattr(fleet_runner, "_write_active_marker", _boom)
    fleet_runner.main()              # stops at the marker write, i.e. AFTER the done-map step
    assert "could not durably mark" in capsys.readouterr().out
    assert json.load(open(tmp_path / "last_run_done.json")) == done


def test_a_fresh_run_still_starts_from_an_empty_done_map():
    src = open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8").read()
    i = src.index("A RESUME KEEPS THE DONE MAP")
    assert "if not args.resume:" in src[i:i + 600]


def test_merge_final_done_map_is_monotonic(tmp_path):
    import relay.fleet_runner as fleet_runner
    (tmp_path / "last_run_done.json").write_text(json.dumps({"jid:old": "DONE"}))
    fleet_runner._merge_final_done_map(str(tmp_path), [])
    assert json.load(open(tmp_path / "last_run_done.json")) == {"jid:old": "DONE"}


# ---- the snapshot ----------------------------------------------------------------------

def test_the_snapshot_embeds_only_this_runs_campaigns(tmp_path):
    _write(tmp_path, _old_ledger(63) + _family(PARENT, n=3, stamp=RUN))
    status = {"workers": [_worker(PARENT)]}
    payload = fleet_reaper._snapshot_payload(status, {"pid": 1}, {}, RUN, str(tmp_path))
    assert payload["campaigns_scoped"] is True
    assert [c["campaign_id"] for c in payload["campaigns"]] == [_cid(PARENT)]
    assert len(json.dumps(payload)) < 20_000


def test_the_snapshot_is_bounded_even_when_the_run_split_hundreds_of_families(tmp_path):
    rows = []
    for j in range(300):
        rows += _family("this run goal %d" % j, n=200, stamp=RUN)
    _write(tmp_path, rows)
    payload = fleet_reaper._snapshot_payload({"workers": []}, {"pid": 1}, {}, RUN, str(tmp_path))
    assert len(payload["campaigns"]) == fr.SNAPSHOT_MAX_CAMPAIGNS
    assert all(len(c["children"]) <= fr.SNAPSHOT_MAX_CHILDREN for c in payload["campaigns"])
    assert len(json.dumps(payload)) < 200_000


def test_a_snapshot_with_no_evidence_embeds_no_campaigns(tmp_path):
    _write(tmp_path, _old_ledger(63))
    payload = fleet_reaper._snapshot_payload({"workers": []}, {"pid": 1}, {}, "rNEW_a0",
                                             str(tmp_path))
    assert payload["campaigns"] == []


# ---- new headers carry the stamp -------------------------------------------------------

def test_new_campaign_headers_are_stamped_with_the_run_id():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    assert '"run_id": run_id, "ts": round(time.time(), 1)' in src
    fams = fanout.campaigns_from_ledger(
        [json.dumps(_header(PARENT, 1, stamp=RUN)), json.dumps(_header(PARENT, 1, stamp="rB_a0"))])
    assert fams[_cid(PARENT)]["run_ids"] == [RUN, "rB_a0"]
    assert fanout.campaigns_from_ledger(
        [json.dumps(_header(PARENT, 1))])[_cid(PARENT)]["run_ids"] == []
