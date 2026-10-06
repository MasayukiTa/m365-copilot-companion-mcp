# -*- coding: utf-8 -*-
"""Nested fan-out: the depth setting, its guard, and the plumbing behind it.

DEFAULT STAYS DEPTH 1. A child that ends as FANOUT counts as finished, so its split proposal
would be read by its parent's merge as its answer. Until nested merging exists the setting is
honoured only up to depth 1 (`fanout.HIERARCHICAL_MERGE_READY` is False and
`effective_max_depth()` is capped). The tests below prove two things:

  * with the default setting, and with the setting at 2 or 3 while the guard is False, every
    output of the split helpers equals what they produced before this setting existed (a digest
    taken from the previous implementation over a fixed set of inputs);
  * with the guard patched on, the plumbing works at depth 2: the gates, the tree keys, the
    grandchild's own scope, the aggregation depth, budget refusal and resume.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout as fo                     # noqa: E402
from relay import fanout_budget as fb              # noqa: E402
from relay import relay_fleet as rf                # noqa: E402
from relay import fleet_resume as fres             # noqa: E402
from tools import settings_keys as SK              # noqa: E402

GOAL = "売上データを集計して報告書を作成する。形式はCSV。"
STEPS = ["1月分のデータを取得して集計する", "2月分のデータを取得して集計する",
         "3月分のデータを取得して集計する"]


def _set_depth(monkeypatch, value):
    """Make the settings reader return `value` (None = key absent)."""
    from relay import fleet_runner as FR
    real = FR._settings_int

    def fake(key, default):
        if key == fo.DEPTH_SETTING_KEY:
            return value if value is not None else default
        return real(key, default)
    monkeypatch.setattr(FR, "_settings_int", fake)


def _digest(obj):
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _outputs():
    """Every split helper's output over a fixed set of inputs."""
    out = {"top": fo.child_goals(GOAL, STEPS, parent_task_id="t-parent", cwd="C:/w")}
    cid = out["top"][0]["campaign_id"]
    out["deeper"] = [fo.child_goals(GOAL, STEPS, parent_task_id="x", depth=d,
                                    parent_campaign_id=cid, parent_subtask_index=2, root_id=cid)
                     for d in (1, 2, 3)]
    out["pair"] = fo.child_goals("別の目標です。詳しく調べてください。", STEPS[:2], campaign_id="cX")
    recs = [{"outcome": "DONE", "subtask_index": 1, "result": "ok"},
            {"outcome": "STUCK", "subtask_index": 2, "result": ""}]
    out["agg"] = fo.aggregation_goal(GOAL, recs, campaign_id=cid, parent_task_id="t-parent",
                                     cwd="C:/w")
    out["agg_checks"] = fo.aggregation_goal(GOAL, recs[:1], campaign_id=cid,
                                            parent_checks=[{"type": "reply_contains", "needle": "x"}])
    out["ledger"] = fo.campaigns_from_ledger([
        json.dumps({"kind": "campaign", "campaign_id": cid, "goal": GOAL, "n": 3, "cwd": "C:/w",
                    "run_id": "r1", "ts": 1.0, "parent_task_id": "t", "parent_campaign_id": "",
                    "root_id": cid}),
        json.dumps({"campaign_id": cid, "task_id": cid + "-1", "subtask_index": 1, "text": "t"})])
    out["gates"] = [fo.may_split_at(d) for d in (0, 1, 2, 3, "x", None)]
    return out


#: sha256 of _outputs() as the implementation before this setting existed produced it
#: (MAX_DEPTH = 1 hard-coded; aggregation depth MAX_DEPTH; no depth key in a header).
GOLDEN = "867e8a3c012cd8a392f662581cfbdfcad1e7fb4289133e6bcfa138fd90c62673"


def test_default_outputs_equal_the_previous_implementation(monkeypatch):
    _set_depth(monkeypatch, None)
    assert _digest(_outputs()) == GOLDEN


@pytest.mark.parametrize("configured", [2, 3])
def test_a_deeper_setting_changes_nothing_while_the_guard_is_off(monkeypatch, configured):
    _set_depth(monkeypatch, configured)
    assert fo.HIERARCHICAL_MERGE_READY is False
    assert fo.configured_max_depth() == configured
    assert fo.effective_max_depth() == 1
    assert _digest(_outputs()) == GOLDEN


def test_the_setting_is_declared_and_clamped(monkeypatch):
    assert SK.effect("fanout_max_depth") == SK.EACH_GATE and SK.default("fanout_max_depth") == 1
    for raw, want in ((None, 1), (0, 1), (-4, 1), (1, 1), (2, 2), (3, 3), (9, 3)):
        _set_depth(monkeypatch, raw)
        assert fo.configured_max_depth() == want, raw


def test_the_report_says_why_the_effective_depth_is_lower(monkeypatch):
    _set_depth(monkeypatch, 3)
    assert fo.depth_report() == {"configured": 3, "effective": 1,
                                 "reason": "hierarchical merge setting is off",
                                 "hierarchical_merge": "off"}
    _set_depth(monkeypatch, 1)
    assert fo.depth_report() == {"configured": 1, "effective": 1, "reason": "",
                                 "hierarchical_merge": "off"}
    monkeypatch.setattr(fo, "HIERARCHICAL_MERGE_READY", True)
    _set_depth(monkeypatch, 3)
    assert fo.depth_report() == {"configured": 3, "effective": 3, "reason": "",
                                 "hierarchical_merge": "on"}


def test_the_snapshot_carries_the_report(monkeypatch):
    from relay import fleet_runner as FR
    _set_depth(monkeypatch, 2)
    assert FR._fanout_depth_block() == {"fanout_depth": fo.depth_report()}


# ----------------------------------------------------------------- guard on: depth 2
@pytest.fixture
def nested(monkeypatch):
    monkeypatch.setattr(fo, "HIERARCHICAL_MERGE_READY", True)
    _set_depth(monkeypatch, 2)


def test_the_gates_follow_the_configured_depth(monkeypatch):
    monkeypatch.setattr(fo, "HIERARCHICAL_MERGE_READY", True)
    for depth_setting, allowed in ((1, [0]), (2, [0, 1]), (3, [0, 1, 2])):
        _set_depth(monkeypatch, depth_setting)
        assert [d for d in range(5) if fo.may_split_at(d)] == allowed
        assert fo.child_goals(GOAL, STEPS, parent_task_id="x", depth=max(allowed)) != []
        assert fo.child_goals(GOAL, STEPS, parent_task_id="x", depth=max(allowed) + 1) == []


def test_the_setting_is_read_each_time_not_at_import(monkeypatch):
    monkeypatch.setattr(fo, "HIERARCHICAL_MERGE_READY", True)
    _set_depth(monkeypatch, 1)
    assert fo.child_goals(GOAL, STEPS, depth=1) == []
    _set_depth(monkeypatch, 2)
    assert fo.child_goals(GOAL, STEPS, depth=1) != []


def test_a_child_splits_and_the_grandchildren_carry_the_tree_keys(nested):
    top = fo.child_goals(GOAL, STEPS, parent_task_id="t-parent")
    cid = top[0]["campaign_id"]
    child = top[1]
    grand = fo.child_goals(child["text"], ["2月の東日本を集計する", "2月の西日本を集計する"],
                           parent_task_id=child["task_id"], depth=child["depth"],
                           parent_campaign_id=cid, parent_subtask_index=child["subtask_index"],
                           root_id=child["root_id"])
    assert len(grand) == 2
    for i, g in enumerate(grand, 1):
        assert g["depth"] == 2 and g["role"] == "subtask" and g["subtask_index"] == i
        assert g["parent_campaign_id"] == cid and g["parent_subtask_index"] == 2
        assert g["root_id"] == cid and g["parent_task_id"] == child["task_id"]
        assert g["campaign_id"] != cid and g["task_id"].startswith(g["campaign_id"])


def _first_blocks(text):
    return [m.start() for m in re.finditer(re.escape("【この会話が担当する範囲"), text)]


def test_a_grandchild_carries_only_its_own_scope_and_the_parents_as_reference(nested, monkeypatch):
    monkeypatch.setattr(fo, "HIERARCHICAL_MERGE_READY", True)
    _set_depth(monkeypatch, 3)
    top = fo.child_goals(GOAL, STEPS, parent_task_id="t")
    child = top[1]
    grand = fo.child_goals(child["text"], ["東日本を集計する担当です", "西日本を集計する担当です"],
                           parent_task_id=child["task_id"], depth=1,
                           parent_campaign_id=child["campaign_id"], root_id=child["root_id"])[0]
    great = fo.child_goals(grand["text"], ["東日本の店舗を集計する", "東日本の倉庫を集計する"],
                           parent_task_id=grand["task_id"], depth=2,
                           parent_campaign_id=grand["campaign_id"], root_id=grand["root_id"])[0]
    for g, own, parent_step in ((grand, "東日本を集計する担当です", STEPS[1]),
                                (great, "東日本の店舗を集計する", "東日本を集計する担当です")):
        assert len(_first_blocks(g["text"])) == 1            # exactly one scope block: its own
        assert own in g["text"].split("【この会話が担当する範囲")[1]
        assert parent_step in g["text"]                       # the immediate parent, as reference
        assert g["text"].startswith(GOAL)
    # the grandparent's step does not travel two levels down
    assert STEPS[1] not in great["text"]
    assert STEPS[0] not in grand["text"] and STEPS[2] not in grand["text"]


def test_a_grandchilds_ledger_shows_its_own_scope(nested):
    top = fo.child_goals(GOAL, STEPS, parent_task_id="t")
    child = top[1]
    grand = fo.child_goals(child["text"], ["東日本を集計する担当です", "西日本を集計する担当です"],
                           parent_task_id=child["task_id"], depth=1,
                           parent_campaign_id=child["campaign_id"], root_id=child["root_id"])[1]
    ledger = rf.goal_ledger(grand["text"])
    scope_line = [ln for ln in ledger.splitlines() if ln.startswith("担当範囲:")]
    assert scope_line and "西日本を集計する担当です" in scope_line[0]
    assert "2/2" in scope_line[0]
    # even a legacy text that nests two blocks reports the innermost
    legacy = (child["text"] + "\n\n【この会話が担当する範囲 — 全体の 2/2】\n内側の担当です\n\n"
              "上の範囲だけを担当してください。他の範囲は別の会話が並行して担当しているので、"
              "手を出さないこと。")
    block, _rest = rf._split_scope_block(legacy)
    assert "内側の担当です" in block and STEPS[1] not in block


def test_the_aggregation_depth_is_the_splitting_workers_plus_one(nested):
    recs = [{"outcome": "DONE", "subtask_index": 1, "result": "a"}]
    assert fo.aggregation_goal(GOAL, recs, campaign_id="c")["depth"] == fo.MAX_DEPTH
    assert fo.aggregation_goal(GOAL, recs, campaign_id="c", depth=1)["depth"] == 1
    assert fo.aggregation_goal(GOAL, recs, campaign_id="c", depth=2)["depth"] == 2
    top = fo.child_goals(GOAL, STEPS, parent_task_id="t")
    grand = fo.child_goals(top[0]["text"], STEPS[:2], parent_task_id=top[0]["task_id"], depth=1,
                           parent_campaign_id=top[0]["campaign_id"], root_id=top[0]["root_id"])
    assert fo.aggregation_goal(top[0]["text"], recs, campaign_id=grand[0]["campaign_id"],
                               depth=grand[0]["depth"])["depth"] == 2


def test_a_merge_worker_never_splits(nested):
    w = rf.RelayWorker({"text": "g", "role": "aggregator", "depth": 1}, "w0", fanout=True,
                       spawn_fn=lambda *a, **k: None)
    assert w._fanout_capable is False and w.fanout is False


def test_a_child_worker_may_split_only_below_the_effective_depth(monkeypatch, tmp_path):
    spawn = lambda *a, **k: None          # noqa: E731
    _set_depth(monkeypatch, 2)
    child = {"text": "g", "role": "subtask", "depth": 1}
    assert rf.RelayWorker(dict(child), "w0", fanout=True, spawn_fn=spawn)._fanout_capable is False
    monkeypatch.setattr(fo, "HIERARCHICAL_MERGE_READY", True)
    assert rf.RelayWorker(dict(child), "w0", fanout=True, spawn_fn=spawn)._fanout_capable is True
    deep = dict(child, depth=2)
    assert rf.RelayWorker(deep, "w0", fanout=True, spawn_fn=spawn)._fanout_capable is False
    # the mid-run offer re-reads the setting: lowering it takes the offer away
    w = rf.RelayWorker(dict(child), "w0", fanout=True, spawn_fn=spawn)
    _set_depth(monkeypatch, 1)
    assert w._ask_for_a_midrun_split() is False


def test_the_split_site_names_the_workers_place_in_the_tree(nested):
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("A NESTED SPLIT (depth > 0)")
    body = src[i:src.index("if kids and self._spawn_fn:", i)]
    assert '"parent_campaign_id": _env.campaign_id' in body
    assert '"parent_subtask_index": self.subtask_index' in body
    assert '(_env.metadata or {}).get("root_id")' in body
    assert "root_id=_tree" not in body and "**_tree" in body       # nothing for a top-level split
    assert '**({"root_id": _tree["root_id"]} if _tree else {})' in body


# ----------------------------------------------------------------- budget at depth 2
def _rows(n_total, active_each=True):
    return [{"root_id": "R", "status": "running" if active_each else "done", "turn": 1}
            for _ in range(n_total)]


def test_a_nested_split_is_charged_to_the_root_and_a_refusal_runs_directly():
    import time
    lim = {"total": 8, "active": 3, "turns": 400, "wall_min": 120}
    steps = ["a" * 10, "b" * 10, "c" * 10]
    camps = [{"kind": "campaign", "campaign_id": "R", "root_id": "R", "n": 1, "ts": time.time()}]
    # room for all three: roomy tree
    use, why = fb.apply_budget(steps, "R", _rows(2, False), camps, lim, min_children=2,
                               self_active=1)
    assert use == steps and why == ""
    # the tree already holds as many workers as it may: refused -> [] (the worker runs it itself)
    use, why = fb.apply_budget(steps, "R", _rows(7, False), camps, lim, min_children=2,
                               self_active=1)
    assert use == [] and "limit" in why
    # three active workers besides the requester: refused on concurrency
    use, why = fb.apply_budget(steps, "R", _rows(4, True), camps, lim, min_children=2,
                               self_active=1)
    assert use == [] and "active" in why
    # the requester itself is not counted against the active limit
    use, _ = fb.apply_budget(steps, "R", _rows(3, True), camps, lim, min_children=2,
                             self_active=1)
    assert use == steps


def test_the_coordinators_grant_takes_the_root_and_defaults_to_the_campaign():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("def _grant_children(")
    body = src[i:src.index("_spawn_children.grant = _grant_children", i)]
    assert 'root_id=""' in body.splitlines()[0]
    assert "root_id or cid" in body and "self_active" in body


def test_a_refused_grant_takes_the_run_directly_branch():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("A NESTED SPLIT (depth > 0)")
    body = src[i:i + 4500]
    assert "kids = (fanout_mod.child_goals(" in body and "if steps else [])" in body
    assert "分割予算の上限のため単独実行に切り替え" in body


# ----------------------------------------------------------------- ledger header and resume
def test_a_nested_header_records_the_depth_and_the_default_header_does_not():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    assert '**({"depth": kids[0]["depth"]} if int(' in src
    header = {"kind": "campaign", "campaign_id": "c", "goal": "g", "n": 2}
    assert "depth" not in fo.campaigns_from_ledger([json.dumps(header)])["c"]
    fam = fo.campaigns_from_ledger([json.dumps(dict(header, depth=2))])["c"]
    assert fam["depth"] == 2


def test_a_resumed_degraded_child_reads_its_depth_from_the_header(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    lines = [{"kind": "campaign", "campaign_id": "cN", "goal": "g", "n": 2, "run_id": "r1",
              "depth": 2, "cwd": "C:/w"},
             {"campaign_id": "cN", "task_id": "cN-1", "subtask_index": 1, "text": "step one"}]
    top = [{"kind": "campaign", "campaign_id": "cT", "goal": "g", "n": 2, "run_id": "r1"},
           {"campaign_id": "cT", "task_id": "cT-1", "subtask_index": 1, "text": "step one"}]
    (state / "campaigns.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines + top) + "\n", encoding="utf-8")
    goals, degraded = fres.resume_children_goals(str(state), done_map={}, log=lambda *_: None,
                                                 scope={"cN", "cT"})
    by = {g["campaign_id"]: g for g in goals}
    assert degraded == 2
    assert by["cN"]["depth"] == 2 and by["cT"]["depth"] == 1


def test_a_merge_queued_for_a_nested_family_uses_that_familys_depth():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("_agg = fanout_mod.aggregation_goal(")
    body = src[i:src.index("_camp[\"merged\"] = True", i)]
    assert '**({"depth": _camp["depth"]}' in body
    assert 'campaigns[cid]["depth"] = int(kids[0]["depth"])' in src


# ----------------------------------------------------------------- the cockpit (source)
def _read(*p):
    return open(os.path.join(REPO, *p), encoding="utf-8").read()


def test_the_cockpit_depth_constants_equal_the_python_side():
    cs = _read("ui", "EffortPolicy.cs")
    assert 'public const string Key = "%s";' % fo.DEPTH_SETTING_KEY in cs
    assert re.search(r"public const int Low = %d;" % fo.DEPTH_SETTING_BOUNDS[0], cs)
    assert re.search(r"public const int High = %d;" % fo.DEPTH_SETTING_BOUNDS[1], cs)
    assert re.search(r"public const int Default = %d;" % SK.default("fanout_max_depth"), cs)


def test_the_cockpit_depth_box_saves_through_savekey_only_and_does_not_refire():
    src = _read("ui", "FleetCockpit.cs")
    # lives in the gear popup (BuildSettingsPanel), NOT in the header
    assert "col.Children.Add(FanoutDepthControl());" in src
    assert "ctrls.Children.Add(FanoutDepthControl());" not in src
    assert len(re.findall(r'SaveKey\("fanout_max_depth", ', src)) == 1
    assert re.search(r'case "fanout_max_depth":\s*case "fanout_max_total":', src)
    m = re.search(r"UIElement FanoutDepthControl\(\).*?\n    }\n", src, re.S)
    assert m and "File." not in m.group(0) and "if (sel == _fdVal) return;" in m.group(0)
    assert "FanoutDepthView.Clamp(sel)" in m.group(0)
    assert "FanoutDepthView.ParseLine(ln)" in src                 # settings load, clamped
    m = re.search(r"void PaintFanoutDepth\(\).*?\n    }\n", src, re.S)
    assert m and "!Equals(ComboVal(_fdBox), _fdVal.ToString())" in m.group(0)


def test_the_screen_words_the_runners_report_not_its_own_selection():
    src = _read("ui", "FleetCockpit.cs")
    m = re.search(r"void PaintFanoutDepthInEffect\(.*?\n    }\n", src, re.S)
    assert m and 'Obj(root, "fanout_depth")' in m.group(0)
    assert "FanoutDepthView.Describe(conf, eff," in m.group(0)
    assert "PaintFanoutDepthInEffect(root);" in src
    py = _read("relay", "fleet_runner.py")
    assert '{"fanout_depth": _fo.depth_report()}' in py
    ep = _read("ui", "EffortPolicy.cs")
    assert "System.Windows" not in ep and "PresentationFramework" not in ep
