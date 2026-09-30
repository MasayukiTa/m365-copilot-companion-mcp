# -*- coding: utf-8 -*-
"""Phase 2 of the interrupted-run resume design (docs/private/20260930_fleet_interrupted_resume_design.md).

G1  a FANOUT parent is resume-done iff its campaign header is on disk
G2  unfinished campaign children are re-queued at resume; child ledger lines carry the goal
G3  `merge_done` is written when the aggregator ends DONE, and the rehydrate rules use it
    resume source = live marker OR newest pending snapshot; loop guard (resume_gate)
    free-space ring + faulthandler log; retention leaves `.fleet/interrupted/` alone

Hermetic: tmp_path fleet directories, injected clocks, injected liveness.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout, fleet_resume as fr  # noqa: E402

GB = 1024 ** 3
PARENT = "1-3 month mail listing"


def _rows(tmp_path, rows):
    with io.open(str(tmp_path / "campaigns.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _header(cid, goal=PARENT, n=3, **kw):
    d = {"kind": "campaign", "campaign_id": cid, "goal": goal, "n": n, "cwd": "C:/w",
         "checks": [], "partial": ""}
    d.update(kw)
    return d


def _child(cid, i, full=True, text=None):
    text = text or "child %d of %s" % (i, cid)
    row = {"campaign_id": cid, "task_id": "%s-%d" % (cid, i), "subtask_index": i, "text": text}
    if full:
        row["goal"] = {"text": text, "cwd": "C:/w", "campaign_id": cid, "role": "subtask",
                       "subtask_index": i, "subtask_of": 3, "depth": 1,
                       "metadata": {"effort": "low"}}
    return row


# ---- reader: the new marker kinds are never children ------------------------------------

def test_marker_lines_are_not_counted_as_children():
    cid = "cR"
    lines = [json.dumps(r) for r in [
        _header(cid), _child(cid, 1),
        {"kind": "merged", "campaign_id": cid, "agg_key": "k1"},
        {"kind": "child_result", "campaign_id": cid, "subtask_index": 1, "outcome": "DONE",
         "result": "x"},
        {"kind": "merge_requeued", "campaign_id": cid, "attempt": 1},
        {"kind": "merge_done", "campaign_id": cid}]]
    fam = fanout.campaigns_from_ledger(lines)[cid]
    assert len(fam["children"]) == 1
    assert fam["merged"] and fam["merge_done"] and fam["merge_requeued"] == 1
    assert fam["agg_key"] == "k1" and len(fam["child_results"]) == 1


def test_marker_lines_written_before_the_header_survive_it():
    cid = "cR"
    lines = [json.dumps(r) for r in [{"kind": "merge_done", "campaign_id": cid},
                                     _header(cid), _header(cid)]]
    assert fanout.campaigns_from_ledger(lines)[cid]["merge_done"] is True


# ---- G1 ---------------------------------------------------------------------------------

def _fr_mod():
    import relay.fleet_runner as fleet_runner
    return fleet_runner


def _ledger(tmp_path, texts):
    fleet_runner = _fr_mod()
    fleet_runner._write_goals_ledger(str(tmp_path), list(texts), time.time())
    return fleet_runner


def test_fanout_parent_with_a_header_is_resume_done(tmp_path):
    fleet_runner = _ledger(tmp_path, [PARENT, "other goal"])
    cid = fanout.campaign_id_for(PARENT)
    _rows(tmp_path, [_header(cid)])
    key = fleet_runner._goal_key(PARENT)
    (tmp_path / "last_run_done.json").write_text(json.dumps({key: "FANOUT"}))
    remainder, n, total = fleet_runner._resume_goals(str(tmp_path))
    assert [g["text"] for g in remainder] == ["other goal"] and (n, total) == (1, 2)


def test_fanout_parent_without_a_header_is_requeued(tmp_path):
    fleet_runner = _ledger(tmp_path, [PARENT])
    (tmp_path / "campaigns.jsonl").write_text("")            # header write failed / never happened
    (tmp_path / "last_run_done.json").write_text(
        json.dumps({fleet_runner._goal_key(PARENT): "FANOUT"}))
    remainder, n, _ = fleet_runner._resume_goals(str(tmp_path))
    assert [g["text"] for g in remainder] == [PARENT] and n == 1


def test_a_header_for_another_goal_does_not_finish_this_parent(tmp_path):
    fleet_runner = _ledger(tmp_path, [PARENT])
    _rows(tmp_path, [_header(fanout.campaign_id_for("something else"))])
    (tmp_path / "last_run_done.json").write_text(
        json.dumps({fleet_runner._goal_key(PARENT): "FANOUT"}))
    assert fleet_runner._resume_goals(str(tmp_path))[1] == 1


def test_fanout_is_recorded_in_the_done_map_but_never_downgrades_done(tmp_path):
    fleet_runner = _fr_mod()

    class W:
        def __init__(self, outcome):
            self.outcome, self.goal, self.jid = outcome, PARENT, None
    d = str(tmp_path)
    fleet_runner._update_done_map(d, [W("FANOUT")])
    key = fleet_runner._goal_key(PARENT)
    assert json.load(open(os.path.join(d, "last_run_done.json")))[key] == "FANOUT"
    fleet_runner._update_done_map(d, [W("DONE")])
    fleet_runner._update_done_map(d, [W("FANOUT")])
    assert json.load(open(os.path.join(d, "last_run_done.json")))[key] == "DONE"
    assert "FANOUT" not in fleet_runner._RESUME_SUCCESS_OUTCOMES


def test_the_two_key_functions_agree():
    fleet_runner = _fr_mod()
    for g in ("a", " a ", {"text": "b"}, {"text": "b", "jid": "J1"}):
        assert fr.goal_resume_key(g) == fleet_runner._goal_resume_key(g)


# ---- G2 ---------------------------------------------------------------------------------

def test_unfinished_children_are_requeued_and_done_ones_are_not(tmp_path):
    cid = "cKIDS"
    _rows(tmp_path, [_header(cid), _child(cid, 1), _child(cid, 2), _child(cid, 3)])
    done = {fr.goal_resume_key({"text": "child 1 of cKIDS"}): "DONE"}
    goals, degraded = fr.resume_children_goals(str(tmp_path), done, log=lambda m: None)
    assert [g["subtask_index"] for g in goals] == [2, 3] and degraded == 0
    assert goals[0]["metadata"] == {"effort": "low"} and goals[0]["cwd"] == "C:/w"


def test_old_child_lines_fall_back_to_text_and_header_cwd_and_are_logged(tmp_path):
    cid = "cOLD"
    _rows(tmp_path, [_header(cid), _child(cid, 1, full=False), _child(cid, 2, full=False)])
    logged = []
    goals, degraded = fr.resume_children_goals(str(tmp_path), {}, log=logged.append)
    assert degraded == 2 and len(logged) == 2 and "degraded" in logged[0]
    assert all(g["degraded"] and g["cwd"] == "C:/w" and g["role"] == "subtask" for g in goals)


def test_a_child_result_line_covers_a_degraded_child_whose_text_hash_cannot_match(tmp_path):
    cid = "cOLD"
    _rows(tmp_path, [_header(cid), _child(cid, 1, full=False), _child(cid, 2, full=False),
                     {"kind": "child_result", "campaign_id": cid, "subtask_index": 1,
                      "outcome": "DONE", "result": "r"}])
    goals, _ = fr.resume_children_goals(str(tmp_path), {}, log=lambda m: None)
    assert [g["subtask_index"] for g in goals] == [2]


def test_a_merged_campaign_re_queues_nothing(tmp_path):
    cid = "cM"
    _rows(tmp_path, [_header(cid), _child(cid, 1), {"kind": "merge_done", "campaign_id": cid}])
    assert fr.resume_children_goals(str(tmp_path), {}, log=lambda m: None)[0] == []


def test_a_family_without_a_header_re_queues_nothing(tmp_path):
    _rows(tmp_path, [_child("cX", 1)])
    assert fr.resume_children_goals(str(tmp_path), {}, log=lambda m: None)[0] == []


def test_the_child_ledger_line_carries_the_whole_goal():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("def _spawn_children(")
    body = src[i:src.index("\n    def _queue_ready_merges", i)]
    assert '"goal": k' in body


def test_a_resumed_run_does_not_requeue_a_child_the_ledger_already_carries(tmp_path, monkeypatch):
    """Second resume: the goals ledger holds the children the first resume queued."""
    fleet_runner = _fr_mod()
    cid = "cTWICE"
    kid = _child(cid, 1)["goal"]
    fleet_runner._write_goals_ledger(str(tmp_path), [kid], time.time())
    _rows(tmp_path, [_header(cid, n=1), _child(cid, 1)])
    remainder, _, _ = fleet_runner._resume_goals(str(tmp_path))
    extra, _ = fr.resume_children_goals(str(tmp_path), {}, log=lambda m: None)
    have = {fleet_runner._goal_resume_key(g) for g in remainder}
    assert [k for k in extra if fleet_runner._goal_resume_key(k) not in have] == []


# ---- G3 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("fam,done,want", [
    ({"merge_done": True, "merged": True}, {}, "drop"),
    ({"merged": False}, {}, "carry"),
    ({"merged": True}, {}, "drop"),                      # legacy `merged` line: no agg_key
    ({"merged": True, "agg_key": "K"}, {}, "reissue"),
    ({"merged": True, "agg_key": "K", "merge_requeued": 1}, {}, "drop"),
    ({"merged": True, "agg_key": "K"}, {"K": "DONE"}, "drop"),
    ({"merged": True, "agg_key": "K"}, {"K": "STUCK"}, "reissue"),
])
def test_rehydrate_decision(fam, done, want):
    assert fr.rehydrate_decision(fam, done) == want


def _rf():
    import relay.relay_fleet as rf
    return rf


def test_campaigns_from_disk_applies_the_rules(tmp_path):
    rf = _rf()
    t = tmp_path / "transcripts"
    t.mkdir()
    _rows(tmp_path, [
        _header("cDONE"), {"kind": "merged", "campaign_id": "cDONE"},
        {"kind": "merge_done", "campaign_id": "cDONE"},
        _header("cQUEUED"), {"kind": "merged", "campaign_id": "cQUEUED", "agg_key": "K"},
        _header("cTWICE"), {"kind": "merged", "campaign_id": "cTWICE", "agg_key": "K"},
        {"kind": "merge_requeued", "campaign_id": "cTWICE", "attempt": 1},
        _header("cLEGACY"), {"kind": "merged", "campaign_id": "cLEGACY"},
        _header("cOPEN")])
    out = rf._campaigns_from_disk(str(t))
    assert sorted(out) == ["cOPEN", "cQUEUED"]
    assert out["cQUEUED"]["requeue_merge"] is True and out["cOPEN"]["requeue_merge"] is False


# ---- G3 at the production seam (same harness as test_the_recovered_family_reaches_a_merge) ----

def _seam():
    spec = importlib.util.spec_from_file_location(
        "seam_helpers", os.path.join(REPO, "relay", "test_the_recovered_family_reaches_a_merge.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _ledger_rows(tmp_path):
    return [json.loads(l) for l in (tmp_path / "campaigns.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


def test_merge_done_is_written_when_the_aggregator_finishes(tmp_path, monkeypatch):
    h = _seam()
    res = h._run(tmp_path, monkeypatch, h._header(n=2), h._orphan_children(2))
    assert len(h._aggregators(res)) == 1
    rows = _ledger_rows(tmp_path)
    kinds = [r.get("kind") for r in rows if r.get("campaign_id") == h.CID]
    assert kinds.count("merge_done") == 1
    assert kinds.index("merged") < kinds.index("merge_done")
    assert kinds.count("child_result") == 2
    merged = [r for r in rows if r.get("kind") == "merged"][0]
    assert merged.get("agg_key")


def _finished_children_rows(h, n=2):
    rows = h._header(n=n, merged=True)
    for r in rows:
        if r.get("kind") == "merged":
            r["agg_key"] = "AGGKEY"          # written by the merge_done-aware writer
    for i in range(1, n + 1):
        rows.append({"kind": "child_result", "campaign_id": h.CID, "subtask_index": i,
                     "outcome": "DONE", "result": "answer %d" % i})
    return rows


def test_a_merge_queued_but_never_finished_is_reissued_exactly_once(tmp_path, monkeypatch):
    h = _seam()
    res = h._run(tmp_path, monkeypatch, _finished_children_rows(h), [])
    assert len(h._aggregators(res)) == 1, "the lost merge was not re-issued"
    kinds = [r.get("kind") for r in _ledger_rows(tmp_path) if r.get("campaign_id") == h.CID]
    assert kinds.count("merge_requeued") == 1 and kinds.count("merge_done") == 1
    # a second death after the re-issue: the cap is one, the merge is not issued again
    tmp2 = tmp_path / "second"
    tmp2.mkdir()
    rows2 = _finished_children_rows(h) + [{"kind": "merge_requeued", "campaign_id": h.CID, "attempt": 1}]
    res2 = h._run(tmp2, monkeypatch, rows2, [])
    assert h._aggregators(res2) == []


def test_merge_done_short_circuits_a_rehydrated_family(tmp_path, monkeypatch):
    h = _seam()
    rows = _finished_children_rows(h) + [{"kind": "merge_done", "campaign_id": h.CID}]
    assert h._aggregators(h._run(tmp_path, monkeypatch, rows, [])) == []


def test_a_merge_finished_in_the_done_map_is_not_reissued(tmp_path, monkeypatch):
    h = _seam()
    rows = _finished_children_rows(h)
    rows = [dict(r) for r in rows]
    for r in rows:
        if r.get("kind") == "merged":
            r["agg_key"] = "AGGKEY"
    (tmp_path / "last_run_done.json").write_text(json.dumps({"AGGKEY": "DONE"}))
    tdir = h._crashed_run(tmp_path, rows)
    h._browserless(monkeypatch)
    res = h.run_relay_fleet(h._FakeContext(), [], "http://agent", max_concurrent=4, poll_s=0,
                            transcript_dir=tdir, notify=lambda *a, **k: None, fanout=False)
    assert h._aggregators(res) == []


# ---- the loop guard -----------------------------------------------------------------------

NOW = 2_000_000.0


def test_gate_basics():
    ok = lambda rec, **kw: fr.resume_gate(rec, kw.pop("now", NOW), kw.pop("free", 50 * GB),
                                          kw.pop("floor", None), kw.pop("sig", ""), **kw)
    assert ok({"state": "pending"}) == (True, "ok")
    assert ok({"state": "pending", "stop_requested": True})[1] == "stop_requested"
    assert ok({"state": "pending"}, coordinator_live=True)[1] == "coordinator_live"
    assert ok({"state": "resumed"})[1] == "state_resumed"
    assert ok({"state": "pending", "resume": {"count": 3}})[1] == "max_resumes"


def test_backoff_doubles_with_the_count_and_uses_the_injected_clock():
    for count, wait in ((1, 600), (2, 1200)):
        rec = {"state": "pending", "resume": {"count": count, "last_ts": NOW}}
        assert fr.resume_gate(rec, NOW + wait - 1, GB, None, "")[1] == "backoff"
        assert fr.resume_gate(rec, NOW + wait, GB, None, "")[0] is True


def test_the_floor_is_only_compared_never_defined():
    assert fr.resume_gate({"state": "pending"}, NOW, 5 * GB, 6, "")[1] == "below_floor"
    assert fr.resume_gate({"state": "pending"}, NOW, 5 * GB, None, "")[0] is True
    assert fr.resume_gate({"state": "pending"}, NOW, 5 * GB, 0, "")[0] is True
    src = open(os.path.join(REPO, "relay", "fleet_resume.py"), encoding="utf-8").read()
    assert not re.search(r"floor\w*\s*=\s*\d", src), "a numeric floor literal appeared"


def test_same_crash_with_no_more_space_is_refused_and_more_space_lets_it_through():
    rec = {"state": "pending", "resume": {"count": 1, "last_ts": 0, "last_signature": "S",
                                          "last_free_bytes": 10 * GB}}
    assert fr.resume_gate(rec, NOW, 10 * GB, None, "S")[1] == "same_crash_no_more_space"
    assert fr.resume_gate(rec, NOW, 11 * GB, None, "S")[0] is True
    assert fr.resume_gate(rec, NOW, 10 * GB, None, "T")[0] is True


def test_disk_full_evidence_needs_more_space_than_at_death():
    rec = {"state": "pending", "interrupted": {"free_bytes_at_detection": 100}}
    assert fr.resume_gate(rec, NOW, 100, None, "", enospc=True)[1] == "disk_full_no_more_space"
    assert fr.resume_gate(rec, NOW, 101, None, "", enospc=True)[0] is True


def test_crash_signature_reads_the_fixed_marker_strings():
    sig, enospc = fr.crash_signature(["x", "OSError: [Errno 28] No space left on device"])
    assert enospc and sig
    assert fr.crash_signature(["fine"]) == ("", False)
    assert fr.crash_signature([], "3221225478", "sqlite3.dll")[0] != ""


# ---- resume source + the resumer script ---------------------------------------------------

def _resumer():
    spec = importlib.util.spec_from_file_location(
        "resume_interrupted_fleet_p2", os.path.join(REPO, "scripts", "win", "resume_interrupted_fleet.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _snapshot(tmp_path, run_id="rAAA_a1", **kw):
    d = tmp_path / "interrupted"
    d.mkdir(exist_ok=True)
    data = {"schema": 1, "run_id": run_id, "state": "pending", "written_ts": 100.0,
            "marker": {"pid": 999999, "resume_argv": ["--state-dir", str(tmp_path)]},
            "interrupted": {"free_bytes_at_detection": 1}, "resume": {"count": 0, "history": []}}
    data.update(kw)
    p = d / (run_id + ".json")
    p.write_text(json.dumps(data))
    return str(p)


def test_the_resumer_resumes_from_the_pending_snapshot_and_counts_it(tmp_path, monkeypatch):
    ri = _resumer()
    path = _snapshot(tmp_path)
    monkeypatch.setattr(ri, "pid_alive", lambda pid: False)
    monkeypatch.setattr(ri, "free_bytes", lambda p: 5 * GB)
    monkeypatch.setattr(ri, "read_floor_gb", lambda: None)
    launched = []
    monkeypatch.setattr(ri.subprocess, "Popen", lambda cmd, **k: launched.append((cmd, k)))
    assert ri.main(["--state-dir", str(tmp_path), "--resume"]) == 0
    assert len(launched) == 1 and launched[0][1]["env"]["MCP_FLEET_RESUME_LINEAGE"] == "rAAA_a1"
    data = json.load(open(path))
    assert data["state"] == "resumed" and data["resume"]["count"] == 1
    assert data["resume"]["last_free_bytes"] == 5 * GB
    # nothing pending any more: a second call does nothing
    assert ri.main(["--state-dir", str(tmp_path), "--resume"]) == 0 and len(launched) == 1


def test_the_resumer_dry_run_changes_nothing_and_the_gate_refusal_is_recorded(tmp_path, monkeypatch, capsys):
    ri = _resumer()
    path = _snapshot(tmp_path, resume={"count": 3, "history": []})
    monkeypatch.setattr(ri, "pid_alive", lambda pid: False)
    monkeypatch.setattr(ri, "free_bytes", lambda p: 5 * GB)
    monkeypatch.setattr(ri, "read_floor_gb", lambda: None)
    monkeypatch.setattr(ri.subprocess, "Popen", lambda *a, **k: pytest.fail("launched"))
    assert ri.main(["--state-dir", str(tmp_path)]) == 0
    assert json.load(open(path))["state"] == "pending"
    assert ri.main(["--state-dir", str(tmp_path), "--resume"]) == 0
    data = json.load(open(path))
    assert data["state"] == "gave_up" and data["resume"]["blocked"]["reason"] == "max_resumes"
    assert "max_resumes" in capsys.readouterr().out


def test_the_resumer_refuses_below_the_operators_floor(tmp_path, monkeypatch):
    ri = _resumer()
    path = _snapshot(tmp_path)
    monkeypatch.setattr(ri, "pid_alive", lambda pid: False)
    monkeypatch.setattr(ri, "free_bytes", lambda p: 1 * GB)
    monkeypatch.setattr(ri, "read_floor_gb", lambda: 4.0)
    monkeypatch.setattr(ri.subprocess, "Popen", lambda *a, **k: pytest.fail("launched"))
    assert ri.main(["--state-dir", str(tmp_path), "--resume"]) == 0
    data = json.load(open(path))
    assert data["state"] == "pending" and data["resume"]["blocked"]["reason"] == "below_floor"


def test_a_live_marker_is_still_the_first_source(tmp_path):
    ri = _resumer()
    _snapshot(tmp_path)
    (tmp_path / "fleet_run_active.json").write_text(json.dumps({"pid": 4242, "resume_argv": ["--x"]}))
    marker, snap = ri.read_resume_source(str(tmp_path))
    assert marker["pid"] == 4242 and snap is None


def test_the_reaper_carries_the_resume_count_to_the_next_snapshot(tmp_path):
    from relay import fleet_reaper
    _snapshot(tmp_path, run_id="rOLD_a1", state="resumed",
              resume={"count": 2, "last_ts": 5.0, "last_signature": "S", "history": [{}, {}],
                      "blocked": {"reason": "x"}})
    payload = fleet_reaper._snapshot_payload(
        {"workers": []}, {"pid": 7, "resume_lineage": "rOLD_a1"}, {}, "rNEW_a1", str(tmp_path))
    assert payload["resume"]["count"] == 2 and payload["resume"]["lineage"] == "rOLD_a1"
    assert "blocked" not in payload["resume"]
    plain = fleet_reaper._snapshot_payload({"workers": []}, {"pid": 7}, {}, "rX_a1", str(tmp_path))
    assert plain["resume"]["count"] == 0
    hostile = fleet_reaper._snapshot_payload(
        {"workers": []}, {"pid": 7, "resume_lineage": "..\\..\\x"}, {}, "rY_a1", str(tmp_path))
    assert hostile["resume"]["count"] == 0


def test_the_coordinator_marker_records_its_lineage(tmp_path, monkeypatch):
    fleet_runner = _fr_mod()
    monkeypatch.setenv("MCP_FLEET_RESUME_LINEAGE", "rOLD_a1")
    fleet_runner._write_active_marker(str(tmp_path), argv=["--x"])
    assert json.load(open(tmp_path / "fleet_run_active.json"))["resume_lineage"] == "rOLD_a1"
    monkeypatch.delenv("MCP_FLEET_RESUME_LINEAGE")
    fleet_runner._write_active_marker(str(tmp_path), argv=["--x"])
    assert "resume_lineage" not in json.load(open(tmp_path / "fleet_run_active.json"))


# ---- free-space ring ------------------------------------------------------------------

def test_the_ring_never_grows_and_survives_ten_thousand_writes(tmp_path):
    ring = fr.FreeSpaceRing(str(tmp_path / fr.RING_FILE))
    size = fr.RING_SLOTS * fr.RING_SLOT_BYTES
    assert os.path.getsize(ring.path) == size == 32768
    for i in range(10_000):
        assert ring.sample(123_456_789_012 + i, 1_000_000_000_000, 6.0, pid=99999, now=1000.0 + i)
    assert os.path.getsize(ring.path) == size and ring.failures == 0
    rows = fr.read_ring(ring.path)
    assert len(rows) == fr.RING_SLOTS
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)
    assert rows[-1]["ts"] == 1000.0 + 9999 and rows[-1]["free_bytes"] == 123_456_789_012 + 9999
    ring.close()


def test_a_reopened_ring_continues_after_the_newest_sample(tmp_path):
    p = str(tmp_path / fr.RING_FILE)
    a = fr.FreeSpaceRing(p)
    for i in range(10):
        a.sample(i, 100, None, pid=1, now=10.0 + i)
    a.close()
    b = fr.FreeSpaceRing(p)
    b.sample(777, 100, None, pid=2, now=100.0)
    b.close()
    rows = fr.read_ring(p)
    assert [r["free_bytes"] for r in rows][-2:] == [9, 777] and len(rows) == 11


def test_a_failed_write_is_swallowed_and_counted(tmp_path):
    import errno
    ring = fr.FreeSpaceRing(str(tmp_path / fr.RING_FILE))

    class Boom:
        def seek(self, *a):
            pass

        def write(self, *a):
            raise OSError(errno.ENOSPC, "No space left on device")

        def close(self):
            pass
    ring._fh = Boom()
    assert ring.sample(1, 2, None) is False and ring.sample(1, 2, None) is False
    assert ring.failures == 2
    dead = fr.FreeSpaceRing(str(tmp_path / "nodir" / "x"))
    assert dead.sample(1, 2, None) is False and dead.failures >= 1


def test_below_floor_is_null_without_a_floor_and_a_plain_comparison_with_one():
    st = lambda free, floor: fr.disk_status(free, 10 * GB, floor, 1.0, 0)
    assert st(GB, None)["below_floor"] is None and st(GB, 0)["below_floor"] is None
    assert st(GB, 0)["floor_gb"] is None
    assert st(GB, 2)["below_floor"] is True and st(3 * GB, 2)["below_floor"] is False
    assert st(None, 2)["below_floor"] is None


def test_the_coordinator_snapshot_reports_the_disk_block(tmp_path, monkeypatch):
    fleet_runner = _fr_mod()
    monkeypatch.setattr(fleet_runner, "_FREE_RING", None)
    monkeypatch.setattr(fleet_runner, "_LAST_DISK", {})
    fleet_runner._start_forensics(str(tmp_path))
    try:
        fleet_runner._sample_free_space(str(tmp_path), 0.0, now=500.0, force=True)
        block = fleet_runner._disk_block(0.0)
        assert block["free_bytes"] > 0 and block["below_floor"] is None and block["log_failures"] == 0
        # throttled: a second call inside the interval writes nothing
        before = len(fr.read_ring(str(tmp_path / fr.RING_FILE)))
        fleet_runner._sample_free_space(str(tmp_path), 0.0, now=510.0)
        assert len(fr.read_ring(str(tmp_path / fr.RING_FILE))) == before
        fleet_runner._sample_free_space(str(tmp_path), 0.0, now=531.0)
        assert len(fr.read_ring(str(tmp_path / fr.RING_FILE))) == before + 1
        assert (tmp_path / ("fault_p%d.log" % os.getpid())).stat().st_size == fr.FAULT_LOG_BYTES
    finally:
        import faulthandler
        faulthandler.disable()
        if fleet_runner._FREE_RING is not None:
            fleet_runner._FREE_RING.close()
        if fr._fault_fh:
            fr._fault_fh.close()


# ---- faulthandler leaves a trace of a native crash ----------------------------------------

def test_a_native_crash_leaves_a_trace_in_the_pre_allocated_fault_log(tmp_path):
    code = ("import sys, faulthandler; sys.path.insert(0, %r);"
            "from relay import fleet_resume as fr;"
            "assert fr.enable_fault_log(%r, pid=4242);"
            "faulthandler._sigsegv()" % (REPO, str(tmp_path)))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60)
    assert proc.returncode != 0
    body = (tmp_path / "fault_p4242.log").read_bytes().replace(b"\0", b"")
    assert b"atal" in body and b"_sigsegv" in body or b"most recent call first" in body, body[:300]


# ---- retention leaves the resume state alone ---------------------------------------------

def test_retention_apply_never_touches_the_snapshot_dir_or_the_forensic_files(tmp_path):
    from relay import fleet_retention as R
    old = time.time() - 400 * 86400
    snap = tmp_path / "interrupted"
    snap.mkdir()
    keep = [snap / "rX_a1.json", tmp_path / "fault_p1234.log", tmp_path / fr.RING_FILE,
            tmp_path / "campaigns.jsonl", tmp_path / "last_run_done.json"]
    keep[0].write_text('{"state": "pending"}')
    keep[1].write_bytes(b"\0" * fr.FAULT_LOG_BYTES)
    keep[2].write_bytes(b"\0" * (fr.RING_SLOTS * fr.RING_SLOT_BYTES))
    keep[3].write_text("{}\n")
    keep[4].write_text("{}")
    for p in keep + [snap]:
        os.utime(str(p), (old, old))
    sizes = {p: p.stat().st_size for p in keep}
    R.apply(str(tmp_path), now=time.time() + 10 * 365 * 86400.0, dry_run=False)
    for p in keep:
        assert p.exists(), "retention removed %s" % p.name
        assert p.stat().st_size == sizes[p], "retention rewrote %s" % p.name
    assert snap.is_dir()
