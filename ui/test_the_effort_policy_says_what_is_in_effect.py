# -*- coding: utf-8 -*-
"""The cockpit shows what the effort policy REALLY is, not what its own combo says.

`relay/effort_policy.py` resolves env MCP_EFFORT_POLICY > settings.txt effort_policy > off and
the runner writes the answer into status.json. `ui/EffortPolicy.cs` (WPF-free, shipped) parses
the setting line and words that report; `ui/FleetCockpit.cs` only calls it.

## What this runs (EXECUTED)

`ui/EffortPolicy.cs` compiled with csc beside the test-only driver
`ui/harness/EffortPolicyHarness.cs` (a console exe), fed JSON cases:

* ParseMode: `effort_policy=on`, a BOM, case, invalid values (keep the default), and that
  `effort=on` does NOT match the key;
* Describe / ConflictText in ja and en, for source env / settings / default, no report -> no
  text, and an env override that differs from the selected value is worded as a conflict;
* BadgeText / BadgeTip: absent level = no badge, the last switch is labelled record-only.
A mutation check swaps the env and settings source words in a copy of the shipped file and
requires the same cases to FAIL, so the cases cannot pass vacuously.

## What is NOT executed

The WPF side (the combo, the amber warning, the pill). Those are asserted as SOURCE below
(string/regex on FleetCockpit.cs): the load branch, SaveKey, the no-refire guard, the each_gate
entry, the status.json read, the badge call, and the Build line. `test_both_windows_can_be_constructed.py`
proves the cockpit still builds and constructs with EffortPolicy.cs in its Build line.

Skips only on a non-Windows host, or a missing csc unless REQUIRE_CSC=1 (CI), which fails.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

UI = os.path.join(REPO, "ui")
FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")
SHIPPED = os.path.join(UI, "EffortPolicy.cs")
HARNESS = os.path.join(UI, "harness", "EffortPolicyHarness.cs")


def _cockpit():
    with open(os.path.join(UI, "FleetCockpit.cs"), encoding="utf-8-sig") as fh:
        return fh.read()


# ── source assertions (no csc needed) ─────────────────────────────────────────────────────

def test_the_shipped_file_has_no_wpf():
    src = open(SHIPPED, encoding="utf-8").read()
    assert "System.Windows" not in src and "PresentationFramework" not in src


def test_the_build_line_ships_it():
    line = [ln for ln in open(os.path.join(UI, "rebuild_ui.ps1"), encoding="utf-8")
            if re.match(r'\s*Build\s+"FleetCockpit"', ln)]
    assert line and '"EffortPolicy.cs"' in line[0]
    assert "EffortPolicyHarness" not in line[0]


def test_the_cockpit_loads_persists_and_does_not_refire():
    src = _cockpit()
    assert "EffortPolicyView.ParseMode(ln)" in src                     # settings load
    assert "SaveKey(EffortPolicyView.Key, _effortPolicy)" in src       # persist
    # the paint assigns only when different, so SelectionChanged does not re-fire
    m = re.search(r"void PaintEffortPolicy\(\).*?\n    }\n", src, re.S)
    assert m and re.search(
        r"if \(!Equals\(ComboVal\(_effortPolicyBox\), _effortPolicy\)\) ComboSelectVal", m.group(0))
    # lives in the gear popup (BuildSettingsPanel), NOT in the header
    assert "col.Children.Add(EffortPolicyControl());" in src
    assert "ctrls.Children.Add(EffortPolicyControl());" not in src


def test_the_timing_table_says_each_gate():
    src = _cockpit()
    assert re.search(r'case "effort_policy":\s*return "each_gate";', src)
    # ...and is not also in a wrong group (a second label would be a compile error anyway)
    assert src.count('case "effort_policy":') == 1


def test_the_screen_reads_the_runners_report_not_its_own_combo():
    src = _cockpit()
    m = re.search(r"void PaintEffortPolicyInEffect\(.*?\n    }\n", src, re.S)
    assert m
    body = m.group(0)
    assert 'Obj(root, "effort_policy")' in body and "EffortPolicyView.Describe(" in body
    assert "EffortPolicyView.ConflictText(" in body and "Warning" in body
    assert re.search(r"RenderCards\(.*?PaintEffortPolicyInEffect\(root\)", src, re.S)
    # the cockpit never hands the env override to a child itself (comments aside)
    assert "MCP_EFFORT_POLICY" not in re.sub(r"//[^\n]*", "", src)


def test_the_worker_badge_is_drawn_from_the_additive_fields():
    src = _cockpit()
    assert "BuildEffortBadge(w)" in src
    m = re.search(r"Border BuildEffortBadge\(.*?\n    }\n", src, re.S)
    assert m and 'S(w, "effort_level")' in m.group(0) and "if (text == null) return null;" in m.group(0)


# ── the compiled file, run ────────────────────────────────────────────────────────────────

pytestmark_nt = pytest.mark.skipif(
    os.name != "nt", reason="the code under test is C# compiled by the .NET Framework csc")


def _require_csc():
    return os.environ.get("REQUIRE_CSC", "").strip() == "1"


def _build(out_dir, shipped):
    if not os.path.isfile(CSC):
        msg = "csc.exe is not at %s" % CSC
        if _require_csc():
            pytest.fail(msg + " and REQUIRE_CSC=1")
        pytest.skip(msg)
    out = os.path.join(str(out_dir), "EffortPolicyHarness.exe")
    r = childproc.run([CSC, "/nologo", "/target:exe", "/out:" + out,
                       "/r:" + os.path.join(FW, "System.Web.Extensions.dll"),
                       shipped, HARNESS], timeout=300)
    assert r.returncode == 0 and os.path.isfile(out), (
        "csc could not build EffortPolicy.cs with its harness (rc=%s):\n%s\n%s"
        % (r.returncode, r.stdout, r.stderr))
    return out


def _run(exe, cases, workdir):
    cp = os.path.join(str(workdir), "cases.json")
    rp = os.path.join(str(workdir), "results.json")
    with open(cp, "w", encoding="utf-8") as fh:
        json.dump(cases, fh, ensure_ascii=False)
    r = childproc.run([exe, cp, rp], timeout=60)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    with open(rp, encoding="utf-8") as fh:
        return json.load(fh)


def _c(op, **kw):
    kw["op"] = op
    return kw


#: (case, expected "text", expected "tip" or None-not-checked)
CASES = [
    (_c("parse", line="effort_policy=on"), "on", None),
    (_c("parse", line="effort_policy=shadow"), "shadow", None),
    (_c("parse", line="effort_policy=off"), "off", None),
    (_c("parse", line="\ufeffeffort_policy= Shadow \r"), "shadow", None),
    (_c("parse", line="effort_policy=ON"), "on", None),
    (_c("parse", line="effort_policy=banana"), None, None),       # invalid -> keep default (off)
    (_c("parse", line="effort_policy="), None, None),
    (_c("parse", line="effort=on"), None, None),                   # a different key
    (_c("parse", line="effort=auto"), None, None),
    (_c("describe", mode="shadow", source="settings", conflict=False, ja=True),
     "有効: シャドウ (設定)", None),
    (_c("describe", mode="shadow", source="settings", conflict=False, ja=False),
     "In effect: shadow (settings)", None),
    (_c("describe", mode="on", source="env", conflict=True, ja=True),
     "有効: オン (環境変数)", None),
    (_c("describe", mode="off", source="default", conflict=False, ja=False),
     "In effect: off (default)", None),
    (_c("describe", mode="", source="default", conflict=False, ja=True), None, None),
    (_c("describe", mode="bogus", source="env", conflict=False, ja=False), None, None),
    (_c("conflict", env="on", selected="off", conflict=True, ja=True),
     "環境変数 MCP_EFFORT_POLICY=on が設定 off を上書き中", None),
    (_c("conflict", env="on", selected="off", conflict=True, ja=False),
     "env MCP_EFFORT_POLICY=on overrides this setting (off)", None),
    (_c("conflict", env="on", selected="off", conflict=False, ja=False), None, None),
    (_c("badge", level="max", source="parent", last=None, ja=True), "推論 max",
     "source: parent"),
    (_c("badge", level="max", source="parent", last=None, ja=False), "effort max",
     "source: parent"),
    (_c("badge", level="max", source="policy",
        last={"turn": 4, "from": "auto", "to": "max", "reason": "refuted"}, ja=False),
     "effort max", "source: policy; last: auto->max (refuted) (shadow)"),
    (_c("badge", level="max", source="policy",
        last={"turn": 4, "from": "auto", "to": "max", "reason": "refuted"}, ja=True),
     "推論 max", "source: policy; last: auto->max (refuted) (記録のみ)"),
    (_c("badge", level="", source="run", last=None, ja=True), None, None),   # absent = no badge
]


def _failures(exe, tmp):
    got = _run(exe, [c for c, _, _ in CASES], tmp)
    assert len(got) == len(CASES)
    bad = []
    for (case, text, tip), r in zip(CASES, got):
        if r.get("text") != text:
            bad.append((case, "text", r.get("text"), text))
        if tip is not None and r.get("tip") != tip:
            bad.append((case, "tip", r.get("tip"), tip))
    return bad


@pytestmark_nt
def test_the_compiled_view_says_what_is_in_effect(tmp_path):
    exe = _build(tmp_path, SHIPPED)
    assert _failures(exe, tmp_path) == []


@pytestmark_nt
def test_swapping_env_and_settings_words_is_caught(tmp_path):
    """Mutation: a copy of the shipped file where the env and settings source words are swapped
    must fail the same cases -- otherwise they prove nothing about precedence wording."""
    src = open(SHIPPED, encoding="utf-8").read()
    a = 'case "env": return ja ? "環境変数" : "environment";'
    b = 'case "settings": return ja ? "設定" : "settings";'
    assert a in src and b in src
    mutated = src.replace(a, 'case "env": return ja ? "設定" : "settings";') \
                 .replace(b, 'case "settings": return ja ? "環境変数" : "environment";')
    mdir = tmp_path / "mut"
    mdir.mkdir()
    mpath = str(mdir / "EffortPolicy.cs")
    with open(mpath, "w", encoding="utf-8-sig") as fh:
        fh.write(mutated)
    exe = _build(mdir, mpath)
    assert _failures(exe, mdir), "the swapped wording passed every case"
    shutil.rmtree(str(mdir), ignore_errors=True)


# -- split-group line: nesting, usage, depth cap (GroupTreeView in the same shipped file) --------

def _g(**kw):
    g = {"campaign_id": "cmp1", "children_total": 3,
         "children": {"queued": 0, "running": 1, "done": 2, "failed": 0, "interrupted": 0},
         "merge_state": "pending", "merge_label": "子の完了待ち",
         "ledger": {"task": "THE LONG GOAL SENTENCE", "constraints": ["c1"], "tokens": ["t1"]}}
    g.update(kw)
    return g


def _tb(used, lim=None, rid="cmp1"):
    lim = lim or {"total": 24, "active": 3, "turns": 400, "wall_min": 120}
    return {"tree_budget": {rid: dict(used, limits=lim)}}


FLAT_JA = "分割グループ 子3件: 実行中 1 · 完了 2 / 統合: 子の完了待ち"
FLAT_EN = "Split group, 3 parts: running 1 · done 2 / merge: 子の完了待ち"
NESTED = dict(depth=0, root_id="cmp1", parent_group_id=None, child_group_ids=["cmp2"],
              descendant_count=2, descendant_turns=9, orphan=False)
USE = {"total": 5, "active": 1, "turns": 40, "wall_min": 12}


def _gl(g, ja=True):
    return dict(_c("group_line", group=g, ja=ja))


def _ge(root, g, ja=True):
    return dict(_c("group_extra", root=root, group=g, ja=ja))


def _run_cases(tmp_path, cases):
    return _run(_build(tmp_path, SHIPPED), cases, tmp_path)


@pytestmark_nt
def test_a_flat_group_reads_exactly_as_before(tmp_path):
    flat = _g()
    r = _run_cases(tmp_path, [_gl(flat), _gl(flat, False), _ge({"groups": [flat]}, flat),
                              _ge({"groups": [flat], "tree_budget": {}}, flat)])
    assert r[0]["text"] == FLAT_JA and r[0]["indent"] == 0
    assert r[1]["text"] == FLAT_EN
    assert r[2]["text"] is None and r[3]["text"] is None      # nothing extra on a flat status.json
    assert "LONG GOAL" not in json.dumps(r, ensure_ascii=False)


@pytestmark_nt
def test_a_nested_group_is_indented_and_counts_its_sub_groups(tmp_path):
    inner = _g(depth=1, root_id="cmp1", parent_group_id="cmp1", child_group_ids=[],
               descendant_count=0, orphan=False)
    outer = _g(merge_state="waiting_on_subgroups", merge_label="下位グループの統合待ち", **NESTED)
    r = _run_cases(tmp_path, [_gl(outer), _gl(outer, False), _gl(inner)])
    assert r[0]["text"] == ("分割グループ 子3件: 実行中 1 · 完了 2 / 統合: 下位グループの統合待ち"
                            " / 下位グループ 1件 (全部で 2件)")
    assert r[1]["text"] == ("Split group, 3 parts: running 1 · done 2 / merge: waiting on sub-groups"
                            " / sub-groups: 1 (2 in all)")
    assert r[0]["indent"] == 0 and r[2]["indent"] == 14
    assert r[2]["text"] == FLAT_JA                              # a leaf group has no sub-group text


@pytestmark_nt
def test_an_orphan_and_an_unknown_merge_are_said_plainly(tmp_path):
    orphan = _g(depth=0, root_id="zzz", orphan=True, merge_state="unknown", merge_label="不明")
    r = _run_cases(tmp_path, [_gl(orphan), _gl(orphan, False)])
    assert r[0]["text"] == FLAT_JA.replace("子の完了待ち", "不明") + " / 親グループが見つかりません"
    assert r[1]["text"].endswith("merge: unknown / parent group not found")


@pytestmark_nt
def test_the_usage_line_colours_at_80_and_100_percent(tmp_path):
    g = _g(**NESTED)
    lim = {"total": 20, "active": 10, "turns": 100, "wall_min": 100}
    cases = [_ge(_tb(USE, lim), g),
             _ge(_tb(dict(USE, total=10, turns=50), lim), g),       # 50%
             _ge(_tb(dict(USE, total=17), lim), g),                 # 85%
             _ge(_tb(dict(USE, total=20), lim), g),                 # 100%
             _ge(_tb(dict(USE, turns=250), lim), g),                # over
             _ge(_tb(USE, lim), g, False)]
    r = _run_cases(tmp_path, cases)
    assert [x["level"] for x in r[:5]] == [0, 0, 1, 2, 2]
    assert r[0]["text"] == "使用量 総数 5/20 · 同時 1/10 · ターン 40/100 · 分 12/100"
    assert r[5]["text"] == "usage total 5/20 · active 1/10 · turns 40/100 · min 12/100"


@pytestmark_nt
def test_the_usage_line_belongs_to_the_root_group_only(tmp_path):
    inner = _g(depth=1, root_id="cmp1", parent_group_id="cmp1")
    r = _run_cases(tmp_path, [_ge(_tb(USE), inner), _ge(_tb(USE, rid="other"), _g(**NESTED))])
    assert r[0]["text"] is None and r[1]["text"] is None


@pytestmark_nt
def test_a_capped_depth_is_noted_and_an_uncapped_one_is_not(tmp_path):
    g = _g(**NESTED)
    capped = {"fanout_depth": {"configured": 3, "effective": 1, "reason": "x"}}
    ok = {"fanout_depth": {"configured": 2, "effective": 2, "reason": ""}}
    both = dict(capped, **_tb(USE))
    r = _run_cases(tmp_path, [_ge(capped, g), _ge(capped, g, False), _ge(ok, g), _ge(both, g),
                              _ge({"fanout_depth": {"configured": 3}}, g)])
    assert r[0]["text"] == "深さは 1 までに制限中: 階層統合は未有効"
    assert r[1]["text"] == "depth capped at 1: hierarchical merge not enabled"
    assert r[2]["text"] is None and r[4]["text"] is None        # equal / missing field
    assert r[3]["text"].startswith("使用量 総数 5/24") and r[3]["text"].endswith("階層統合は未有効")


def test_the_cockpit_draws_the_group_line_from_the_view_and_never_the_goal():
    src = _cockpit()
    m = re.search(r"UIElement BuildGroupLine\(.*?\n    }\n", src, re.S)
    assert m
    body = m.group(0)
    assert "GroupTreeView.IndentPx(g)" in body and "GroupExtraText(w, out xLevel)" in body
    assert 'S(led, "task")' in body                              # the capped ledger, as before
    assert 'S(w, "goal")' not in body and "GroupTreeView.Line(" in src
    assert "ShowScaleToast" not in body and "MessageBox" not in body
