"""Nested split groups in relay.family_view: tree keys, merge_state, caps, text rendering.

Hermetic and pure: worker dicts of the status.json shape, campaigns.jsonl lines as strings.
"""
import json

from relay import family_view as fv

NEW_KEYS = {"parent_group_id", "root_id", "depth", "child_group_ids", "descendant_count",
            "descendant_turns", "orphan"}
SECRET = "ZZ_BODY_MARKER_ZZ"


def _w(**o):
    base = dict(name="", campaign_id="", task_id="", parent_task_id=None, role="",
                subtask_index=None, status="running", outcome="", goal="", turn=0)
    base.update(o)
    return base


def _head(cid, ptid, pcid="", root=""):
    return json.dumps({"kind": "campaign", "campaign_id": cid, "goal": "g " + SECRET, "n": 2,
                       "parent_task_id": ptid, "parent_campaign_id": pcid,
                       "root_id": root or cid})


def _flat(cid="c1", n=3):
    ws = [_w(name="p" + cid, campaign_id=cid, task_id="tp" + cid, role="producer",
             status="done", outcome="FANOUT", goal=SECRET)]
    for i in range(n):
        ws.append(_w(name="k%s%d" % (cid, i), campaign_id=cid, task_id="tk%s%d" % (cid, i),
                     parent_task_id="tp" + cid, role="subtask", status="done", outcome="DONE",
                     turn=2))
    return ws


def _two_level(inner_status="running", inner_outcome="", agg=None):
    """Outer group cO whose slot tk1 fanned out into the nested group cI."""
    ws = [_w(name="po", campaign_id="cO", task_id="tpo", role="producer", status="done",
             outcome="FANOUT"),
          _w(name="a", campaign_id="cO", task_id="tk0", parent_task_id="tpo", role="subtask",
             status="done", outcome="DONE", turn=3),
          _w(name="b", campaign_id="cO", task_id="tk1", parent_task_id="tpo", role="subtask",
             status="done", outcome="FANOUT", turn=1)]
    for i in range(2):
        ws.append(_w(name="i%d" % i, campaign_id="cI", task_id="ti%d" % i, parent_task_id="tk1",
                     role="subtask", status=inner_status, outcome=inner_outcome, turn=5))
    if agg:
        ws.append(_w(name="iagg", campaign_id="cI", task_id="tia", parent_task_id="tk1",
                     role="aggregator", status=agg, outcome="DONE" if agg == "done" else ""))
    lines = [_head("cO", "tpo"), _head("cI", "tk1", pcid="cO", root="cO")]
    return ws, lines


def _by_id(groups):
    return {g["group_id"]: g for g in groups}


def test_flat_fleet_is_byte_identical_plus_neutral_new_keys():
    ws = _flat("c1") + _flat("c2", 2)
    new = fv.build_groups(ws, [_head("c1", "tpc1"), _head("c2", "tpc2")])
    old = fv.build_flat_groups(ws, [_head("c1", "tpc1"), _head("c2", "tpc2")])
    stripped = [{k: v for k, v in g.items() if k not in NEW_KEYS} for g in new]
    assert json.dumps(stripped, sort_keys=True) == json.dumps(old, sort_keys=True)
    for g in new:
        assert g["parent_group_id"] is None and g["depth"] == 0 and g["root_id"] == g["group_id"]
        assert g["child_group_ids"] == [] and g["descendant_count"] == 0
        assert g["descendant_turns"] == 0 and g["orphan"] is False
        assert "child_group_ids_truncated" not in g


def test_two_level_tree_nests_the_inner_group():
    ws, lines = _two_level()
    g = _by_id(fv.build_groups(ws, lines))
    o, i = g["cO"], g["cI"]
    assert i["parent_group_id"] == "cO" and i["depth"] == 1 and i["root_id"] == "cO"
    assert not i["orphan"]
    assert o["parent_group_id"] is None and o["depth"] == 0 and o["root_id"] == "cO"
    assert o["child_group_ids"] == ["cI"] and o["descendant_count"] == 1
    assert o["descendant_turns"] == 10 and i["descendant_count"] == 0


def test_waiting_on_subgroups_until_the_inner_merge_is_done():
    ws, lines = _two_level()
    o = _by_id(fv.build_groups(ws, lines))["cO"]
    assert o["merge_state"] == "waiting_on_subgroups" and o["merge_label"]
    assert o["parent"]["display_state"] == "awaiting_children"
    assert o["parent"]["status"] == "done" and o["parent"]["outcome"] == "FANOUT"
    ws, lines = _two_level("done", "DONE", agg="done")
    gs = _by_id(fv.build_groups(ws, lines))
    assert gs["cI"]["merge_state"] == "merged"
    assert gs["cO"]["merge_state"] == "ready"
    assert gs["cO"]["parent"]["display_state"] == "ready_to_merge"


def test_fanout_slot_without_a_nested_group_is_unknown_not_guessed():
    ws, lines = _two_level()
    ws = [w for w in ws if w["campaign_id"] != "cI"]
    o = _by_id(fv.build_groups(ws, [lines[0]]))["cO"]
    assert o["merge_state"] == "unknown" and o["child_group_ids"] == []


def test_unknown_inner_state_propagates_up():
    ws, lines = _two_level()
    ws.append(_w(name="deep", campaign_id="cI", task_id="tdeep", parent_task_id="ti0",
                 role="subtask", status="done", outcome="FANOUT"))
    g = _by_id(fv.build_groups(ws, lines))
    assert g["cI"]["merge_state"] == "unknown"
    assert g["cO"]["merge_state"] == "unknown"


def test_orphan_child_group_is_reported_not_attached():
    ws = [_w(name="x%d" % i, campaign_id="cOrph", task_id="tx%d" % i, parent_task_id="gone",
             role="subtask", status="running", root_id="cRoot") for i in range(2)]
    g = fv.build_groups(ws, [_head("cOrph", "gone", pcid="cMissing", root="cRoot")])[0]
    assert g["parent_group_id"] is None and g["orphan"] is True
    assert g["depth"] == 0 and g["root_id"] == "cRoot"
    assert g["merge_state"] == "pending"


def test_depth_is_capped_and_a_loop_cannot_hang():
    n = fv.MAX_DEPTH + 5
    ws, lines = [], []
    for k in range(n):
        cid = "g%02d" % k
        ptid = "t%02d" % (k - 1) if k else "root"
        lines.append(_head(cid, ptid, pcid="g%02d" % (k - 1) if k else ""))
        ws.append(_w(name="n%d" % k, campaign_id=cid, task_id="t%02d" % k, parent_task_id=ptid,
                     role="subtask", status="done", outcome="FANOUT" if k < n - 1 else "DONE"))
    gs = fv.build_groups(ws, lines)
    assert len(gs) == n and max(g["depth"] for g in gs) <= fv.MAX_DEPTH
    assert any(g["orphan"] for g in gs)
    # a two-group loop terminates and nothing is left depth-less
    loop = [_w(name="a", campaign_id="cA", task_id="ta", parent_task_id="tb", role="subtask"),
            _w(name="b", campaign_id="cB", task_id="tb", parent_task_id="ta", role="subtask")]
    out = fv.build_groups(loop, [_head("cA", "tb", "cB"), _head("cB", "ta", "cA")])
    assert len(out) == 2 and all(isinstance(g["depth"], int) for g in out)


def test_child_group_ids_are_clipped_but_the_count_is_exact():
    n = fv.MAX_CHILD_IDS + 10
    ws = [_w(name="po", campaign_id="cO", task_id="tpo", role="producer", status="done",
             outcome="FANOUT")]
    lines = [_head("cO", "tpo")]
    for k in range(n):
        ws.append(_w(name="s%d" % k, campaign_id="cO", task_id="s%03d" % k, parent_task_id="tpo",
                     role="subtask", status="done", outcome="FANOUT"))
        cid = "i%03d" % k
        ws.append(_w(name="c%d" % k, campaign_id=cid, task_id="c%03d" % k,
                     parent_task_id="s%03d" % k, role="subtask", status="running", turn=1))
        lines.append(_head(cid, "s%03d" % k, "cO"))
    o = _by_id(fv.build_groups(ws, lines))["cO"]
    assert len(o["child_group_ids"]) == fv.MAX_CHILD_IDS
    assert o["child_group_ids_truncated"] == 10
    assert o["descendant_count"] == n and o["descendant_turns"] == n


def test_no_goal_text_in_the_output():
    ws, lines = _two_level()
    long_goal = "Build the report " + "with background " * 300 + SECRET
    lines = [json.dumps(dict(json.loads(x), goal=long_goal)) for x in lines]
    ws[0]["goal"] = long_goal
    assert SECRET not in json.dumps(fv.build_groups(ws, lines), ensure_ascii=False)


def test_render_text_indents_by_depth_and_flat_stays_flat():
    ws, lines = _two_level()
    txt = fv.render_text(fv.build_groups(ws, lines)).splitlines()
    outer = next(x for x in txt if "cO" in x and x.startswith("分割グループ"))
    inner = next(x for x in txt if x.lstrip().startswith("分割グループ cI"))
    assert outer == txt[0] and inner.startswith("    分割グループ")
    assert any("親グループ: cO" in x for x in txt)
    assert any("下位グループ 1 件" in x for x in txt)
    flat_txt = fv.render_text(fv.build_groups(_flat("c1")))
    assert all(not x.startswith(" ") or x.startswith("  ") and not x.startswith("    ")
               for x in flat_txt.splitlines())
    assert flat_txt == fv.render_text(fv.build_flat_groups(_flat("c1")))
