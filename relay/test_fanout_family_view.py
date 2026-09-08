"""Tests for relay.fanout.fanout_family_view -- the derived fan-out family projection.

Pure, no fleet/browser. Feeds worker-snapshot dicts of exactly the shape that
relay/fleet_runner.py already writes into status.json (campaign_id / task_id /
parent_task_id / role / depth / subtask_index / outcome / last) and checks that each
worker is labelled so the cockpit can render the family without inferring anything.
"""
from relay.fanout import fanout_family_view, SUBTASKS_READY


def _fam(**over):
    base = dict(name="", campaign_id="", task_id="", parent_task_id=None,
                role="", depth=0, subtask_index=None, outcome="", last="")
    base.update(over)
    return base


def test_solo_worker_is_solo_and_carries_no_family():
    v = fanout_family_view([_fam(name="w0", outcome="DONE")])
    assert v["w0"]["kind"] == "solo"
    assert v["w0"]["campaign_id"] == ""


def _family(child_outcomes, agg=None, parent_last=""):
    cid = "cmp1"
    ws = [_fam(name="p", campaign_id=cid, task_id="t-p", role="producer",
               outcome="FANOUT", last=parent_last)]
    for i, oc in enumerate(child_outcomes):
        ws.append(_fam(name="c%d" % i, campaign_id=cid, task_id="t-c%d" % i,
                       parent_task_id="t-p", role="subtask", subtask_index=i,
                       depth=1, outcome=oc))
    if agg is not None:
        ws.append(_fam(name="agg", campaign_id=cid, task_id="t-agg",
                       parent_task_id="t-p", role="aggregator", outcome=agg))
    return ws


def test_parent_and_children_are_labelled_with_progress():
    v = fanout_family_view(_family(["DONE", "DONE", "STUCK"]))
    p = v["p"]
    assert p["kind"] == "parent"
    assert p["children_total"] == 3 and p["children_done"] == 2
    assert p["split_proposed_not_run"] is False
    assert v["c0"]["kind"] == "child"
    assert v["c0"]["subtask_of"] == "p"
    assert v["c0"]["subtask_index"] == 0


def test_fanin_state_pending_then_ready_then_merging_then_merged():
    assert fanout_family_view(_family(["DONE", "STUCK"]))["p"]["fanin_state"] == "pending"
    assert fanout_family_view(_family(["DONE", "DONE"]))["p"]["fanin_state"] == "ready"
    merging = fanout_family_view(_family(["DONE", "DONE"], agg="RUNNING"))
    assert merging["p"]["fanin_state"] == "merging"
    assert merging["agg"]["kind"] == "aggregator"
    merged = fanout_family_view(_family(["DONE", "DONE"], agg="DONE"))
    assert merged["p"]["fanin_state"] == "merged"


def test_missing_slices_named_when_a_slice_did_not_finish():
    v = fanout_family_view(_family(["DONE", "STUCK", "DONE"]))
    assert v["p"]["missing_slices"] == [1]


def test_split_proposed_but_no_children_is_flagged_not_solo():
    ws = [_fam(name="p", campaign_id="cmpX", task_id="t-p", role="producer",
               outcome="STUCK", last="...plan...\n%s" % SUBTASKS_READY)]
    v = fanout_family_view(ws)
    assert v["p"]["kind"] == "stalled_parent"
    assert v["p"]["split_proposed_not_run"] is True
    assert v["p"]["kind"] != "solo"


def test_empty_and_malformed_inputs_do_not_raise():
    assert fanout_family_view([]) == {}
    assert fanout_family_view(None) == {}
    v = fanout_family_view([{"name": "w9"}])
    assert v["w9"]["kind"] == "solo"
