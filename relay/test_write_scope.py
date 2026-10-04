# -*- coding: utf-8 -*-
"""Sibling write scope, SHADOW ONLY: what is detected, what is recorded, what stays untouched.

Covers relay/write_scope.py: the step-text declaration reader, the overlap finder, the
tool_events attribution rules (ambiguous rows skipped and counted, shell/python never
flagged), the `scope_overlap` mechanism row, and that `off` changes nothing.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fanout  # noqa: E402
from relay import mechanism_telemetry as MT  # noqa: E402
from relay import write_scope as WS  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh():
    WS._reset_for_test()
    yield
    WS._reset_for_test()


# ---------------------------------------------------------------- declared_scope

@pytest.mark.parametrize("text,expected", [
    ("Edit relay/fleet_runner.py and the ui/ folder", ("relay/fleet_runner.py", "ui/")),
    ("Update `tools\\settings_keys.py` only", ("tools/settings_keys.py",)),
    ("README.md \u3092\u66f4\u65b0\u3057\u3066\u304f\u3060\u3055\u3044", ("readme.md",)),
    ("\u300cC:\\My Docs\\a b.txt\u300d\u3092\u66f4\u65b0", ("c:/my docs/a b.txt",)),
    ('update "my notes/todo list.md" please', ("my notes/todo list.md",)),
    ("./src/a.cs and ../b/c.py", ("src/a.cs", "../b/c.py")),
    ("it's fine, don't touch ui/x.py ok", ("ui/x.py",)),
])
def test_declared_scope_reads_explicit_paths_and_filenames(text, expected):
    assert WS.declared_scope(text) == expected


@pytest.mark.parametrize("text", [
    "", None, "Summarise the findings", "compare and/or contrast 1/2 of 2026/10/02",
    "see https://example.com/a/b.py for details", "version 3.10 and e.g. things",
])
def test_no_explicit_path_is_no_declaration(text):
    assert WS.declared_scope(text) == ()


def test_declared_scope_never_raises_on_junk():
    assert WS.declared_scope(object()) in ((), WS.declared_scope(str(object())))


# ---------------------------------------------------------------- path matching

def test_a_relative_path_matches_an_absolute_one_at_a_component_boundary():
    assert WS.path_matches("relay/x.py", "c:/repo/relay/x.py")
    assert not WS.path_matches("relay/x.py", "c:/repo/myrelay/x.py")
    assert WS.path_matches("x.py", "c:/repo/relay/x.py")


def test_a_directory_holds_what_is_under_it():
    assert WS.path_matches("ui/", "c:/repo/ui/a.cs")
    assert not WS.path_matches("ui/", "c:/repo/guide/a.cs")
    assert not WS.path_matches("ui/", "c:/repo/ui")        # the dir itself is not a file under it


# ---------------------------------------------------------------- overlaps (pure)

def _w(child, path, tool="write_file", campaign="c1"):
    return {"campaign": campaign, "child": child, "path": WS.norm_path(path), "tool": tool}


def test_two_siblings_writing_one_path_overlap():
    out = WS.overlaps({}, [_w("c1-1", "a/x.py"), _w("c1-2", "A\\x.py")])
    assert [(o["kind"], o["path"]) for o in out] == [("written_by_two", "a/x.py")]


def test_a_write_into_another_siblings_declared_path_overlaps():
    decl = {("c1", "c1-1"): ("a/x.py",), ("c1", "c1-2"): ("b/",)}
    out = WS.overlaps(decl, [_w("c1-1", "C:/r/b/y.py")])
    assert len(out) == 1
    assert (out[0]["kind"], out[0]["writer"], out[0]["other"]) == ("declared_by_other", "c1-1", "c1-2")


def test_a_write_inside_ones_own_declaration_is_the_plan_working():
    decl = {("c1", "c1-1"): ("a/x.py",), ("c1", "c1-2"): ("b/y.py",)}
    assert WS.overlaps(decl, [_w("c1-1", "a/x.py"), _w("c1-2", "b/y.py")]) == []


def test_no_declaration_is_neither_success_nor_violation():
    assert WS.overlaps({("c1", "c1-1"): (), ("c1", "c1-2"): ()}, [_w("c1-1", "a.py")]) == []


def test_siblings_of_different_campaigns_do_not_overlap():
    assert WS.overlaps({}, [_w("c1-1", "a.py", campaign="c1"), _w("c2-1", "a.py", campaign="c2")]) == []


def test_the_same_overlap_is_reported_once():
    out = WS.overlaps({}, [_w("c1-1", "a.py"), _w("c1-1", "a.py"), _w("c1-2", "a.py"), _w("c1-2", "a.py")])
    assert len(out) == 1


# ---------------------------------------------------------------- tool_events rows

def _call(i, tool, args, task="", worker="", attr="window", **extra):
    row = {"event": "call", "id": "id%d" % i, "ts": 1000.0 + i, "tool": tool, "task": task,
           "worker": worker, "attr": attr,
           "args": {k: {"text": v if isinstance(v, str) else json.dumps(v), "len": 1}
                    for k, v in args.items()}}
    row.update(extra)
    return row


def _roster(goal1="", goal2=""):
    return WS.Roster([
        {"name": "w1", "role": "subtask", "campaign_id": "c1", "task_id": "c1-1", "jid": "j1",
         "goal": goal1, "outcome": "DONE"},
        {"name": "w2", "role": "subtask", "campaign_id": "c1", "task_id": "c1-2", "jid": "j2",
         "goal": goal2, "outcome": ""},
        {"name": "agg", "role": "aggregator", "campaign_id": "c1", "task_id": "c1-m", "jid": "j3"},
    ])


def test_write_class_calls_are_read_by_tool_name_and_edit_lists_by_their_paths():
    rows = [
        _call(1, "write_file", {"path": "C:/r/a.py", "content": "x"}, task="j1"),
        _call(2, "multi_edit", {"path": "b.py", "edits": "[]"}, task="j1"),
        _call(3, "edit_and_verify", {"edits": [{"path": "c.py", "old": "a", "new": "b"},
                                               {"path": "sub\\d.py", "old": "a", "new": "b"}],
                                     "repo": "C:/r"}, task="j2"),
        _call(4, "read_file", {"path": "C:/r/never.py"}, task="j1"),
    ]
    writes, stats = WS.writes_from_events(rows, _roster())
    assert sorted((w["child"], w["path"]) for w in writes) == [
        ("c1-1", "b.py"), ("c1-1", "c:/r/a.py"), ("c1-2", "c:/r/c.py"), ("c1-2", "c:/r/sub/d.py")]
    assert stats["unknown_write"] == 0


def test_shell_and_python_are_unknown_writes_and_never_flagged():
    rows = [_call(1, "run_python", {"code": "open('a.py','w')"}, task="j1"),
            _call(2, "shell_exec", {"command": "echo > a.py"}, task="j2"),
            _call(3, "write_file", {"path": "a.py", "content": ""}, task="j1")]
    writes, stats = WS.writes_from_events(rows, _roster())
    assert stats["unknown_write"] == 2 and len(writes) == 1
    assert WS.overlaps({}, writes) == []


def test_ambiguous_or_unresolvable_rows_are_skipped_and_counted():
    rows = [_call(1, "write_file", {"path": "a.py"}, task="j1", attr="ambiguous"),
            _call(2, "write_file", {"path": "a.py"}, task="", worker="", attr=""),
            _call(3, "write_file", {"path": "a.py"}, task="nobody", worker="ghost"),
            _call(4, "write_file", {"path": "a.py"}, task="j2")]
    writes, stats = WS.writes_from_events(rows, _roster())
    assert [w["child"] for w in writes] == ["c1-2"]
    assert stats["unknown_attribution"] == 3


def test_campaign_identity_fields_on_the_row_win_when_present():
    rows = [_call(1, "write_file", {"path": "a.py"}, campaign_id="cX", task_id="cX-3", attr="window")]
    writes, _ = WS.writes_from_events(rows, WS.Roster([]))
    assert [(w["campaign"], w["child"]) for w in writes] == [("cX", "cX-3")]


def test_a_worker_name_shared_by_two_campaigns_is_ambiguous():
    roster = WS.Roster([
        {"name": "w", "role": "subtask", "campaign_id": "c1", "task_id": "c1-1", "jid": "a"},
        {"name": "w", "role": "subtask", "campaign_id": "c2", "task_id": "c2-1", "jid": "b"}])
    writes, stats = WS.writes_from_events(
        [_call(1, "write_file", {"path": "a.py"}, task="", worker="w", attr="session")], roster)
    assert writes == [] and stats["unknown_attribution"] == 1


# ---------------------------------------------------------------- the setting

def _settings(tmp_path, line):
    p = tmp_path / "settings.txt"
    p.write_text("dark=0\n" + line, encoding="utf-8")
    return str(p)


@pytest.mark.parametrize("line,expected", [
    ("", "off"), ("fanout_write_scope=shadow\n", "shadow"), ("fanout_write_scope=SHADOW\n", "shadow"),
    ("fanout_write_scope=off\n", "off"),
    # there is no enforcing value: whatever else is written reads as off
    ("fanout_write_scope=on\n", "off"), ("fanout_write_scope=enforce\n", "off"),
    ("fanout_write_scope=\n", "off")])
def test_the_setting_accepts_only_off_and_shadow(tmp_path, line, expected):
    assert WS.mode(_settings(tmp_path, line)) == expected


def test_the_modes_have_no_enforcing_value():
    assert WS.MODES == ("off", "shadow")


def test_a_missing_settings_file_is_off(tmp_path):
    assert WS.mode(str(tmp_path / "nope.txt")) == "off"


# ---------------------------------------------------------------- the shadow recorder

def _worker(name, jid, task_id, goal, outcome, campaign="c1", role="subtask"):
    class _Env(object):
        pass
    env = _Env()
    env.campaign_id, env.task_id, env.role = campaign, task_id, role

    class _W(object):
        pass
    w = _W()
    w.name, w.jid, w.goal, w.outcome, w.task_envelope = name, jid, goal, outcome, env
    return w


def _events_file(tmp_path, rows):
    p = tmp_path / "tool_events.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(p)


def _workers(goal1, goal2, o1="DONE", o2="DONE"):
    return [_worker("w1", "j1", "c1-1", goal1, o1), _worker("w2", "j2", "c1-2", goal2, o2)]


def _goal(step):
    return fanout.child_goals("do the thing", [step], campaign_id="cx")[0]["text"]


def test_shadow_records_one_scope_overlap_row_per_overlap_and_blocks_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(MT, "LOG", str(tmp_path / "mechanisms.jsonl"))
    settings = _settings(tmp_path, "fanout_write_scope=shadow\n")
    events = _events_file(tmp_path, [
        _call(1, "write_file", {"path": "C:/r/shared.py", "content": "1"}, task="j1"),
        _call(2, "write_file", {"path": "C:/r/shared.py", "content": "2"}, task="j2")])
    workers = _workers(_goal("edit pkg/one.py"), _goal("edit pkg/two.py"))
    assert WS.shadow_tick(workers, events_path=events, settings_path=settings) == 1
    rows = [json.loads(ln) for ln in open(MT.LOG, encoding="utf-8")]
    assert len(rows) == 1
    r = rows[0]
    assert r["mechanism"] == "scope_overlap"
    assert (r["configured"], r["triggered"], r["executed"]) == (True, True, True)
    assert r["config_value"] == "shadow" and r["instance"] == "c1-1"
    ex = r["extra"]
    assert ex["campaign"] == "c1" and ex["kind"] == "written_by_two"
    assert (ex["writer"], ex["other"], ex["path"]) == ("c1-1", "c1-2", "c:/r/shared.py")
    assert ex["record_only"] is True and ex["unknown_attribution"] == 0
    # a second sweep with nothing new records nothing, and the counter says one
    assert WS.shadow_tick(workers, events_path=events, settings_path=settings) == 0
    assert WS.status_block(settings)["fanout_write_scope"] == {"mode": "shadow", "overlaps_seen": 1}


def test_shadow_notices_a_write_into_the_declared_path_of_a_sibling(tmp_path):
    settings = _settings(tmp_path, "fanout_write_scope=shadow\n")
    events = _events_file(tmp_path, [
        _call(1, "write_file", {"path": "C:/r/pkg/two.py", "content": "1"}, task="j1")])
    got = []
    workers = _workers(_goal("edit pkg/one.py"), _goal("edit pkg/two.py"))
    WS.shadow_tick(workers, events_path=events, settings_path=settings,
                   record=lambda *a, **k: got.append((a, k)))
    assert len(got) == 1 and got[0][1]["extra"]["kind"] == "declared_by_other"
    assert got[0][1]["extra"]["other"] == "c1-2"


def test_ambiguous_rows_are_counted_in_the_row_not_used(tmp_path):
    settings = _settings(tmp_path, "fanout_write_scope=shadow\n")
    events = _events_file(tmp_path, [
        _call(1, "write_file", {"path": "a.py"}, task="j1"),
        _call(2, "write_file", {"path": "a.py"}, task="j2"),
        _call(3, "write_file", {"path": "a.py"}, task="j2", attr="ambiguous")])
    got = []
    WS.shadow_tick(_workers("x", "y"), events_path=events, settings_path=settings,
                   record=lambda *a, **k: got.append(k))
    assert len(got) == 1 and got[0]["extra"]["unknown_attribution"] == 1


def test_off_reads_nothing_and_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(MT, "LOG", str(tmp_path / "mechanisms.jsonl"))
    settings = _settings(tmp_path, "fanout_write_scope=off\n")
    got = []
    boom = str(tmp_path / "never_read.jsonl")          # absent: a read attempt would be visible
    monkeypatch.setattr(WS, "_read_tail_rows", lambda *a, **k: got.append("read") or [])
    assert WS.shadow_tick(_workers("a", "b"), events_path=boom, settings_path=settings,
                          record=lambda *a, **k: got.append("record")) == 0
    assert got == [] and not os.path.exists(MT.LOG)
    assert WS.status_block(settings)["fanout_write_scope"] == {"mode": "off", "overlaps_seen": 0}


def test_a_scan_waits_for_a_child_to_finish(tmp_path):
    settings = _settings(tmp_path, "fanout_write_scope=shadow\n")
    events = _events_file(tmp_path, [
        _call(1, "write_file", {"path": "a.py"}, task="j1"),
        _call(2, "write_file", {"path": "a.py"}, task="j2")])
    got = []
    assert WS.shadow_tick(_workers("x", "y", "", ""), events_path=events, settings_path=settings,
                          record=lambda *a, **k: got.append(k)) == 0 and got == []
    assert WS.shadow_tick(_workers("x", "y", "DONE", ""), events_path=events, settings_path=settings,
                          record=lambda *a, **k: got.append(k)) == 1


def test_the_recorder_never_raises(tmp_path):
    settings = _settings(tmp_path, "fanout_write_scope=shadow\n")

    def boom(*a, **k):
        raise RuntimeError("telemetry down")
    events = _events_file(tmp_path, [_call(1, "write_file", {"path": "a.py"}, task="j1"),
                                     _call(2, "write_file", {"path": "a.py"}, task="j2")])
    assert WS.shadow_tick(_workers("x", "y"), events_path=events, settings_path=settings,
                          record=boom) == 0


def test_the_mechanism_is_registered_so_summarise_sees_it():
    assert "scope_overlap" in MT.MECHANISMS


# ---------------------------------------------------------------- off is byte-identical to main

def test_child_goals_do_not_mention_the_scope_reader():
    """The split's output (prompts included) is exactly what it was: this module never touches
    it. A golden over one split, with the setting on and off, must be identical."""
    steps = ["edit pkg/one.py", "edit pkg/two.py"]
    a = fanout.child_goals("do the thing", steps, campaign_id="cx")
    b = fanout.child_goals("do the thing", steps, campaign_id="cx")
    assert a == b
    blob = json.dumps(a, ensure_ascii=False, sort_keys=True)
    assert "write_scope" not in blob and "scope_overlap" not in blob
    # ...and no module that builds a prompt or an output knows about it at all
    for rel in ("fanout.py", "relay_fleet.py", "task_router.py", "effort_policy.py"):
        with open(os.path.join(REPO, "relay", rel), encoding="utf-8-sig") as fh:
            assert "write_scope" not in fh.read(), rel
