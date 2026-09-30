"""Tests for relay.family_view -- the owner-facing split-group (分割グループ) ledger.

Hermetic and pure: worker dicts of the status.json shape, campaigns.jsonl lines as strings.
"""
import json

from relay import family_view as fv
from relay.fanout import fanout_family_view

LONG_GOAL = ("売上集計を作成してください。" + "背景の説明です。" * 120 +
             "出力は必ず「月次表」という名前にしてください。期限は2026-10-31です。"
             "予算は500万円以内にしてください。")
SECRET_TAIL = "ZZ_BODY_MARKER_ZZ"


def _w(**o):
    base = dict(name="", campaign_id="", task_id="", parent_task_id=None, role="",
                subtask_index=None, status="running", outcome="", goal="")
    base.update(o)
    return base


def _family(kids, agg=None, parent_outcome="FANOUT", cid="c1"):
    ws = [_w(name="p", campaign_id=cid, task_id="tp", role="producer", status="done",
             outcome=parent_outcome, goal=LONG_GOAL + SECRET_TAIL)]
    for i, (st, oc) in enumerate(kids):
        ws.append(_w(name="k%d" % i, campaign_id=cid, task_id="tk%d" % i,
                     parent_task_id="tp", role="subtask", subtask_index=i + 1,
                     status=st, outcome=oc))
    if agg:
        ws.append(_w(name="agg", campaign_id=cid, task_id="ta", parent_task_id="tp",
                     role="aggregator", status=agg[0], outcome=agg[1]))
    return ws


def _one(ws, lines=None):
    gs = fv.build_groups(ws, lines)
    assert len(gs) == 1
    return gs[0]


def test_waiting_parent_is_displayed_waiting_not_finished():
    g = _one(_family([("running", ""), ("done", "DONE"), ("pending", ""), ("running", "")]))
    assert g["parent"]["display_state"] == "awaiting_children"
    assert g["parent"]["display_label"] == "待機中"
    assert g["parent"]["status"] == "done" and g["parent"]["outcome"] == "FANOUT"
    assert g["children"] == {"queued": 1, "running": 2, "done": 1, "failed": 0, "interrupted": 0}
    assert g["children_total"] == 4 and g["merge_state"] == "pending"
    assert g["label"] == "分割グループ c1" and "family" not in json.dumps(g).lower()


def test_merge_state_progression():
    done = [("done", "DONE")] * 3
    assert _one(_family(done))["merge_state"] == "ready"
    assert _one(_family(done))["parent"]["display_state"] == "ready_to_merge"
    assert _one(_family(done, ("running", "")))["parent"]["display_state"] == "merging"
    g = _one(_family(done, ("done", "DONE")))
    assert g["merge_state"] == "merged" and g["parent"]["display_state"] == "done"
    g = _one(_family(done, ("stuck", "STUCK")))
    assert g["merge_state"] == "failed" and g["parent"]["display_state"] == "merge_failed"


def test_merged_line_in_ledger_means_merge_queued():
    lines = [json.dumps({"kind": "campaign", "campaign_id": "c1", "goal": LONG_GOAL, "n": 2}),
             json.dumps({"kind": "merged", "campaign_id": "c1"})]
    assert _one(_family([("done", "DONE")] * 2), lines)["merge_state"] == "merging"


def test_interrupted_child_is_not_failed_and_flags_the_parent():
    g = _one(_family([("interrupted", "INTERRUPTED"), ("done", "DONE"), ("stuck", "STUCK")]))
    assert g["children"]["interrupted"] == 1 and g["children"]["failed"] == 1
    assert g["parent"]["display_state"] == "interrupted"
    assert any("中断" in x for x in g["warnings"])


def test_long_goal_never_reappears_and_output_is_bounded():
    lines = [json.dumps({"kind": "campaign", "campaign_id": "c1", "goal": LONG_GOAL + SECRET_TAIL,
                         "n": 2})]
    gs = fv.build_groups(_family([("running", "")] * 2), lines)
    blob = json.dumps(gs, ensure_ascii=False) + fv.render_text(gs)
    assert SECRET_TAIL not in blob and "背景の説明です。" * 3 not in blob
    lg = gs[0]["ledger"]
    assert len(lg["task"]) <= fv.TASK_CAP
    assert len(lg["constraints"]) <= fv.MAX_CONSTRAINTS
    assert all(len(c) <= fv.CONSTRAINT_CAP for c in lg["constraints"])
    assert len(blob) < 2500


def test_ledger_constraint_tokens_are_reported():
    g = _one(_family([("running", "")] * 2))
    lg = g["ledger"]
    assert lg["task"].startswith("売上集計")
    assert lg["constraint_count"] >= 1
    joined = " ".join(lg["tokens"])
    assert "2026-10-31" in joined or "500万円" in joined or "「月次表」" in joined


def test_injected_ledger_fn_and_failure_are_tolerated():
    ws = _family([("running", "")])
    g = fv.build_groups(ws, None, ledger_fn=lambda goal, jid="": "タスク: T\n固定条件:\n- 「X」を使う\n")[0]
    assert g["ledger"]["task"] == "T" and g["ledger"]["tokens"] == ["「X」"]

    def boom(goal, jid=""):
        raise RuntimeError("x")
    assert fv.build_groups(ws, None, ledger_fn=boom)[0]["ledger"]["task"] == ""


def test_display_state_is_derived_only_and_leaves_inputs_alone():
    ws = _family([("running", ""), ("done", "DONE")])
    before = json.dumps(ws, sort_keys=True)
    out = fv.annotate_display_state(ws)
    assert json.dumps(ws, sort_keys=True) == before
    p = [w for w in out if w["name"] == "p"][0]
    assert p["display_state"] == "awaiting_children"
    assert p["status"] == "done" and p["outcome"] == "FANOUT"
    assert all("display_state" not in w for w in out if w["name"] != "p")
    # the real projection still calls the parent finished-by-design
    assert fanout_family_view(ws)["p"]["kind"] == "parent"


def test_terminal_tuple_is_unchanged():
    from relay.relay_fleet import TERMINAL
    assert "interrupted" not in TERMINAL and "awaiting_children" not in TERMINAL


def test_solo_and_garbage_rows_do_not_raise():
    assert fv.build_groups(None) == []
    assert fv.build_groups([None, 3, {}, _w(name="s", campaign_id="x")]) == []
    assert fv.render_text([]) == ""
    assert fv.annotate_display_state(None) == []


def test_real_goal_ledger_end_to_end_and_render():
    gs = fv.build_groups(_family([("running", ""), ("done", "DONE")]))
    txt = fv.render_text(gs)
    assert "分割グループ c1" in txt and "待機中" in txt and "子 2 件" in txt


def test_read_fleet_dir_tolerates_missing_and_torn(tmp_path):
    assert fv.read_fleet_dir(str(tmp_path)) == ([], [])
    (tmp_path / "status.json").write_text("{torn", encoding="utf-8")
    assert fv.read_fleet_dir(str(tmp_path))[0] == []
    (tmp_path / "status.json").write_text(json.dumps({"workers": _family([("done", "DONE")])}),
                                          encoding="utf-8")
    ws, lines = fv.read_fleet_dir(str(tmp_path))
    assert len(ws) == 2 and lines == []
