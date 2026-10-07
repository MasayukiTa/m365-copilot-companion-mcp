"""relay.task_tree and scripts/tree_report.py: a read-only tree derived from existing rows.

Hermetic: synthetic worker dicts of the status.json shape and campaigns.jsonl lines as strings.
"""
import copy
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import family_view as fv  # noqa: E402
from relay import task_tree as tt  # noqa: E402
from scripts import tree_report as tr  # noqa: E402


def _w(tid, parent=None, role="", status="done", outcome="DONE", turn=1, **o):
    d = dict(name="n" + str(tid), task_id=tid, parent_task_id=parent, role=role,
             status=status, outcome=outcome, turn=turn, campaign_id="", depth=0)
    d.update(o)
    return d


def _flat():
    return [_w("a"), _w("b", turn=4), _w("c", turn=2)]


def _two_level():
    return [
        _w("p", role="producer", outcome="FANOUT", campaign_id="c1", turn=3),
        _w("k1", "p", "subtask", campaign_id="c1", turn=5),
        _w("k2", "p", "subtask", campaign_id="c1", turn=7),
        _w("g1", "k1", "subtask", campaign_id="c1", turn=2),
        _w("m", "p", "aggregator", campaign_id="c1", turn=1),
    ]


CAMP = [json.dumps({"kind": "campaign", "campaign_id": "c1", "goal": "g", "n": 2}),
        json.dumps({"campaign_id": "c1", "task_id": "k1"}),
        json.dumps({"campaign_id": "c1", "task_id": "k2"}),
        json.dumps({"campaign_id": "c1", "task_id": "k3"}),
        json.dumps({"kind": "merged", "campaign_id": "c1"})]


def test_flat_fleet_is_all_roots():
    t = tt.build_tree(_flat())
    assert sorted(t["roots"]) == ["a", "b", "c"]
    assert t["orphans"] == [] and t["cycles"] == []
    n = t["nodes"]["b"]
    assert (n["depth"], n["descendants"], n["subtree_turns"], n["root_id"]) == (0, 0, 4, "b")


def test_two_level_tree_numbers():
    t = tt.build_tree(_two_level(), CAMP)
    n = t["nodes"]
    assert t["roots"] == ["p"]
    assert sorted(n["p"]["children"]) == ["k1", "k2", "m"]
    assert n["p"]["descendants"] == 4
    assert n["p"]["subtree_turns"] == 3 + 5 + 7 + 2 + 1
    assert n["k1"]["subtree_turns"] == 7 and n["k1"]["descendants"] == 1
    assert (n["g1"]["depth"], n["g1"]["root_id"], n["g1"]["parent_id"]) == (2, "p", "k1")
    assert n["m"]["merge_state"] == "merged"
    assert n["p"]["merge_state"] in ("merging", "merged")
    assert t["ledger_only_children"] == 1          # k3 is in the ledger, has no worker row


def test_orphan_is_reported_not_attached():
    ws = [_w("a"), _w("x", "gone"), _w("y", "x", turn=3)]
    t = tt.build_tree(ws)
    assert t["orphans"] == ["x"] and t["roots"] == ["a"]
    assert t["nodes"]["x"]["anomaly"] == "orphan"
    assert t["nodes"]["x"]["parent_id"] == "gone"
    assert t["nodes"]["y"]["root_id"] == "x" and t["nodes"]["x"]["descendants"] == 1


def test_cycle_is_reported_and_not_computed():
    ws = [_w("a", "b"), _w("b", "a"), _w("c", "a"), _w("s", "s"), _w("ok")]
    t = tt.build_tree(ws)
    assert t["cycles"] == [["a", "b"], ["s"]]
    n = t["nodes"]
    assert n["c"]["anomaly"] == "under_cycle" and n["c"]["root_id"] == ""
    assert n["a"]["descendants"] is None and n["a"]["subtree_turns"] is None
    assert t["roots"] == ["ok"]


def test_old_rows_missing_keys_do_not_raise():
    ws = [{"name": "old1"}, {"name": "old2", "status": "running"}, {}, "junk", None,
          {"task_id": 7, "parent_task_id": "", "turn": "x", "depth": "?"}]
    t = tt.build_tree(ws, ["not json", "", "{torn"])
    assert t["unidentified"] == 1                    # the empty dict
    assert "w:old1" in t["nodes"] and "7" in t["nodes"]
    assert t["nodes"]["7"]["turns"] == 0 and t["nodes"]["7"]["declared_depth"] == 0
    assert t["nodes"]["w:old2"]["state"] == "running"


def test_duplicate_ids_counted_last_row_wins():
    t = tt.build_tree([_w("a", turn=1), _w("a", turn=9)])
    assert t["duplicate_ids"] == 1 and t["nodes"]["a"]["turns"] == 9


def test_large_input_is_capped_and_deep_chain_does_not_recurse():
    ws = [_w("n0")] + [_w("n%d" % i, "n%d" % (i - 1)) for i in range(1, 5000)]
    t = tt.build_tree(ws)
    assert t["nodes"]["n0"]["descendants"] == 4999 and t["nodes"]["n4999"]["depth"] == 4999
    c = tt.build_tree(ws, max_nodes=100)
    assert c["truncated"] and c["dropped"] == 4900 and len(c["nodes"]) == 100


def test_slice_changes_no_existing_output():
    ws = _two_level()
    before = copy.deepcopy(ws)
    g0 = fv.build_groups(ws, CAMP, ledger_fn=lambda g, j="": "")
    a0 = fv.annotate_display_state(ws, CAMP)
    tt.build_tree(ws, CAMP)
    assert ws == before                              # inputs never mutated
    assert fv.build_groups(ws, CAMP, ledger_fn=lambda g, j="": "") == g0
    assert fv.annotate_display_state(ws, CAMP) == a0


def test_report_on_status_json_shape(tmp_path):
    ws = _two_level() + [_w("z", "gone", "subtask", goal_hash="h", jid="j1"),
                         _w("y", None, "aggregator", status="stuck", outcome="STUCK",
                            goal_hash="h", jid="j1")]
    (tmp_path / "status.json").write_text(
        json.dumps({"workers": ws, "quota": {"used": 12, "limit": 100}}), encoding="utf-8")
    (tmp_path / "campaigns.jsonl").write_text("\n".join(CAMP) + "\n", encoding="utf-8")
    from relay.family_view import read_fleet_dir
    w, lines = read_fleet_dir(str(tmp_path))
    rep = tr.build_report(w, lines, tr.read_quota(str(tmp_path)))
    for h in ("## Size", "## Retry / duplication", "## Merge", "## Quota pressure", "## Unknown"):
        assert h in rep
    assert "orphans: 1" in rep and "limit: 100" in rep
    assert "goal_hash: 1/2 (50.0%)" in rep
    assert "non-DONE: 1/2 (50.0%)" in rep
    files_before = sorted(os.listdir(tmp_path))
    assert tr.main(["--fleet-dir", str(tmp_path)]) == 0
    assert sorted(os.listdir(tmp_path)) == files_before    # read only


def _hdr(cid, ptid="", pcid="", root=""):
    return json.dumps({"kind": "campaign", "campaign_id": cid, "goal": "g", "n": 2,
                       "parent_task_id": ptid, "parent_campaign_id": pcid, "root_id": root or cid})


def _depth2_fleet(prune_slot=False):
    """Producer rows carry NO task_id and NO campaign_id; children name the campaign id (root
    parent identity) or a slot id. Campaign cB hangs from slot cA-1 of root campaign cA."""
    ws = [_w("", None, "producer", outcome="FANOUT", name="w0", jid="j1"),
          _w("cA-1", "cA", "subtask", campaign_id="cA", outcome="FANOUT"),
          _w("cA-2", "cA", "subtask", campaign_id="cA"),
          _w("cA-merge", "cA", "aggregator", campaign_id="cA"),
          _w("cB-1", "cA-1", "subtask", campaign_id="cB"),
          _w("cB-2", "cA-1", "subtask", campaign_id="cB")]
    if prune_slot:
        ws = [w for w in ws if w["task_id"] != "cA-1"]
    lines = [_hdr("cA", "cA"), _hdr("cB", "cA-1", "cA", "cA")]
    return ws, lines


def test_depth2_fleet_with_empty_producer_ids_has_depth2_and_no_orphans():
    for prune in (False, True):
        ws, lines = _depth2_fleet(prune)
        t = tt.build_tree(ws, lines)
        sh = tt.tree_shape(t)
        assert sh["max_depth"] == 2 and sh["orphans"] == 0 and t["orphans"] == []
        assert t["nodes"]["cB-1"]["root_id"] == "cA" and t["nodes"]["cB-1"]["depth"] == 2
        assert t["nodes"]["cA"]["virtual"] is True and t["nodes"]["cA"]["descendants"] == 5
        assert t["roots"] == ["cA"]
        assert t["nodes"]["cA-1"]["virtual"] is prune


def test_empty_task_id_producer_is_unknown_not_a_root_or_orphan():
    ws, lines = _depth2_fleet()
    t = tt.build_tree(ws, lines)
    assert t["unlinked"] == 1 and "w:w0" in t["nodes"]
    assert "w:w0" not in t["roots"] and "w:w0" not in t["orphans"]
    rep = tr.build_report(ws, lines)
    assert "orphans: 0" in rep and "unplaced rows (no identity beyond a worker name): 1" in rep
    assert "max depth (tree distance from top): 2" in rep


def test_producer_with_campaign_id_takes_it_as_its_identity():
    ws = [_w("", None, "producer", campaign_id="cA", outcome="FANOUT"),
          _w("cA-1", "cA", "subtask", campaign_id="cA")]
    t = tt.build_tree(ws, [_hdr("cA", "cA")])
    assert t["roots"] == ["cA"] and t["nodes"]["cA"]["virtual"] is False
    assert t["orphans"] == [] and t["nodes"]["cA"]["descendants"] == 1


def test_orphan_only_when_the_claimed_parent_is_missing_everywhere():
    ws, lines = _depth2_fleet()
    ws.append(_w("z-1", "zzz-7", "subtask", campaign_id="cZ"))
    t = tt.build_tree(ws, lines)
    assert t["orphans"] == ["z-1"] and t["nodes"]["z-1"]["anomaly"] == "orphan"
    # a header that names a parent campaign nobody recorded does not invent one either
    ws2 = [_w("q-1", "cA-9", "subtask", campaign_id="cQ")]
    t2 = tt.build_tree(ws2, [_hdr("cQ", "cA-9", "cNope", "cNope")])
    assert t2["orphans"] == ["q-1"]


def test_header_loop_cannot_hang_materialisation():
    ws = [_w("a-1", "b-1", "subtask", campaign_id="cA")]
    lines = [_hdr("cA", "b-1", "cB"), _hdr("cB", "a-1", "cA")]
    t = tt.build_tree(ws, lines)
    assert set(t["nodes"]) >= {"a-1"}


def test_report_without_quota_or_files_says_unavailable(tmp_path):
    rep = tr.build_report([], [], tr.read_quota(str(tmp_path)))
    assert "unavailable" in rep and "n/a (0 rows)" in rep


def test_gate_readout_small_sample_is_not_a_judgement_and_unknown_stays_unknown(tmp_path, capsys):
    ws = _two_level()
    lines = list(CAMP)
    g = tr.gate_summary(ws, lines, None)
    assert g["sample_ok"] is False and g["quota"] is None
    rep = tr.build_report(ws, lines, None)
    assert rep.index("## Gate readout") < rep.index("## Size")
    assert "NOT a judgement" in rep and "quota pressure: unknown" in rep
    g2 = tr.gate_summary(ws, lines, {"rpm": 3, "refusals_5m": 0, "note": "x"})
    assert g2["quota"] == {"rpm": 3, "refusals_5m": 0}
    (tmp_path / "status.json").write_text(json.dumps({"workers": ws}), encoding="utf-8")
    assert tr.main(["--fleet-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["split_trees"] == g["split_trees"]
