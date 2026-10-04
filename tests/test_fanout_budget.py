# -*- coding: utf-8 -*-
"""The per-tree fan-out budget: limits, partial grants, fail-closed usage, and "nothing changes".

relay/fanout_budget.py bounds one fan-out tree (a root campaign and its descendants) by total
workers, active workers, turns and wall clock. These tests pin four things:

* grant() and usage_from_status() behave as documented, and unknown data fails CLOSED;
* at the default limits and depth 1 the budget changes NO existing split: for every split size a
  worker may produce (2..MAX_CHILDREN) the steps and the children built from them are identical
  before and after the budget is applied;
* the worker's split path honours a refusal (runs the goal itself, no STUCK) and a partial grant
  (fewer children, no work dropped, never fewer than two);
* the four settings agree across the registry, the Python reader and the cockpit.
"""
from __future__ import annotations

import io
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fanout as fo  # noqa: E402
from relay import fanout_budget as fb  # noqa: E402
from relay import fleet_runner as FR  # noqa: E402
from relay import relay_fleet as rf  # noqa: E402
from tools import settings_keys as SK  # noqa: E402
from tools import settings_path as SP  # noqa: E402

NOW = 1_800_000_000.0


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def _row(root, status="working", turn=1):
    return {"root_id": root, "status": status, "turn": turn}


def _hdr(root, n=3, ts=NOW - 600):
    return {"kind": "campaign", "campaign_id": root, "root_id": root, "n": n, "ts": ts}


LIM = fb.default_limits()


# ---------------------------------------------------------------- grant()

def test_a_fresh_root_is_granted_the_whole_request():
    usage = fb.usage_from_status([], [], now=NOW)
    assert usage == {}
    assert fb.grant("r", 12, usage, LIM) == (12, "")


def test_total_limit_trims_and_keeps_a_merge_slot():
    usage = fb.usage_from_status([_row("r", "done")] * 10, [_hdr("r", 10)], now=NOW)
    granted, why = fb.grant("r", 12, usage, dict(LIM, total=14))
    assert granted == 3                      # 14 - 10 workers - 1 held for the merge
    assert "limit 14" in why


def test_total_limit_exhausted_refuses():
    usage = fb.usage_from_status([_row("r", "done")] * 23, [_hdr("r", 23)], now=NOW)
    assert fb.grant("r", 4, usage, LIM)[0] == 0


@pytest.mark.parametrize("kw,fragment", [
    ({"turns": 10}, "turns"),
    ({"wall_min": 5}, "wall clock"),
    ({"active": 2}, "active"),
])
def test_each_limit_refuses_when_reached(kw, fragment):
    rows = [_row("r", "working", turn=6), _row("r", "working", turn=6)]
    usage = fb.usage_from_status(rows, [_hdr("r", 2, ts=NOW - 600)], now=NOW)
    granted, why = fb.grant("r", 3, usage, dict(LIM, **kw))
    assert granted == 0 and fragment in why


def test_queued_workers_do_not_count_as_active_but_do_count_as_total():
    rows = [_row("r", "pending"), _row("r", "pending"), _row("r", "done")]
    u = fb.usage_from_status(rows, [_hdr("r", 3)], now=NOW)["r"]
    assert (u["total"], u["active"]) == (3, 0)


def test_the_requester_is_not_counted_against_the_active_limit():
    usage = fb.usage_from_status([_row("r")], [_hdr("r", 1)], now=NOW)
    assert fb.grant("r", 3, usage, dict(LIM, active=1))[0] == 0
    assert fb.grant("r", 3, usage, dict(LIM, active=1), self_active=1)[0] == 3


@pytest.mark.parametrize("requested", [0, -1, None, True, 2.5])
def test_nothing_or_nonsense_requested_grants_nothing(requested):
    assert fb.grant("r", requested, {}, LIM)[0] == 0


def test_unknown_usage_fails_closed():
    assert fb.grant("r", 5, None, LIM)[0] == 0
    assert fb.grant("r", 5, "garbage", LIM)[0] == 0
    assert fb.grant("", 5, {}, LIM)[0] == 0
    assert fb.usage_from_status(None, [], now=NOW) is None
    assert fb.usage_from_status([], None, now=NOW) is None        # ledger unreadable
    assert fb.usage_from_status(5, [], now=NOW) is None


def test_a_tree_whose_turns_or_start_time_are_unreadable_cannot_grant_more():
    bad_turn = fb.usage_from_status([{"root_id": "r", "status": "working", "turn": "x"}],
                                    [_hdr("r", 1)], now=NOW)
    assert bad_turn["r"]["known"] is False and fb.grant("r", 3, bad_turn, LIM)[0] == 0
    no_start = fb.usage_from_status([_row("r")], [], now=NOW)
    assert no_start["r"]["wall_min"] is None and fb.grant("r", 3, no_start, LIM)[0] == 0


def test_bad_limits_fall_back_to_the_defaults():
    usage = fb.usage_from_status([], [], now=NOW)
    assert fb.grant("r", 12, usage, None) == (12, "")
    assert fb.grant("r", 12, usage, {"total": "x"}) == (12, "")


# ---------------------------------------------------------------- usage_from_status()

def test_usage_is_derived_per_root_from_rows_and_campaign_lines():
    rows = [_row("a", "working", 3), _row("a", "done", 7), _row("b", "stuck", 2),
            {"status": "working", "turn": 99},               # solo goal: no root, not a tree
            "junk", None]
    lines = [_hdr("a", 4, ts=NOW - 1800), _hdr("a", 2, ts=NOW - 60), _hdr("b", 1, ts=NOW - 120),
             '{"kind": "campaign", "campaign_id": "c", "n": 2, "ts": %d}' % (NOW - 3600),
             {"campaign_id": "a", "task_id": "child line, not a header"}, "not json"]
    u = fb.usage_from_status(rows, lines, now=NOW)
    assert u["a"] == {"total": 6, "active": 1, "turns": 10, "wall_min": 30.0, "known": True}
    assert u["b"]["active"] == 0 and u["b"]["turns"] == 2 and u["b"]["wall_min"] == 2.0
    assert u["c"]["total"] == 2 and u["c"]["wall_min"] == 60.0       # queued, not yet workers
    assert set(u) == {"a", "b", "c"}


def test_rows_from_workers_reads_the_root_the_status_export_reads():
    class _Env(object):
        metadata = {"root_id": "r1"}

    class _W(object):
        status, turn, task_envelope = "working", 4, _Env()

    class _Solo(object):
        status, turn, task_envelope = "done", 1, None

    rows = fb.rows_from_workers([_W(), _Solo()])
    assert rows[0] == {"root_id": "r1", "status": "working", "turn": 4}
    assert rows[1]["root_id"] == ""


def test_read_campaign_rows_distinguishes_absent_from_unreadable(tmp_path):
    assert fb.read_campaign_rows(str(tmp_path / "none.jsonl")) == []
    assert fb.read_campaign_rows("") == []
    p = tmp_path / "campaigns.jsonl"
    p.write_text('{"kind": "campaign", "campaign_id": "a", "n": 2}\nnot json\n', encoding="utf-8")
    assert fb.read_campaign_rows(str(p)) == [{"kind": "campaign", "campaign_id": "a", "n": 2}]
    assert fb.read_campaign_rows(str(tmp_path)) is None              # exists, cannot be read


# ---------------------------------------------------------------- trim / apply

def test_trim_folds_the_tail_so_no_work_is_dropped():
    steps = ["a one", "b two", "c three", "d four"]
    out = fb.trim_steps(steps, 2)
    assert out == ["a one", "b two\nc three\nd four"]
    assert fb.trim_steps(steps, 4) is steps and fb.trim_steps(steps, 9) is steps
    assert fb.trim_steps(steps, 0) == []


def test_a_partial_grant_below_two_is_a_refusal():
    rows = [_row("r", "done")] * 12
    steps = ["s%d long enough" % i for i in range(6)]
    use, why = fb.apply_budget(steps, "r", rows, [_hdr("r", 12)], dict(LIM, total=14), now=NOW)
    assert use == [] and why                                         # room for 1 -> not a split
    use, why = fb.apply_budget(steps, "r", rows, [_hdr("r", 12)], dict(LIM, total=15), now=NOW)
    assert len(use) == 2 and "\n" in use[1]


def test_apply_budget_refuses_when_usage_cannot_be_read():
    use, why = fb.apply_budget(["a b c d e f g h", "i j k l m n o p"], "r", [], None, LIM, now=NOW)
    assert use == [] and "unknown" in why


# ---------------------------------------------------------------- nothing changes at defaults

@pytest.mark.parametrize("n", range(fo.MIN_CHILDREN, fo.MAX_CHILDREN + 1))
def test_default_limits_change_no_single_split_before_after(n):
    """Before: the steps the agent proposed. After: the steps the budget lets through for a
    root that has not split yet (depth 1: it only ever splits once). The children built from
    them are identical too."""
    steps = ["step number %d does a distinct slice of the work" % i for i in range(n)]
    usage = fb.usage_from_status([], [], now=NOW)
    assert fb.grant("croot", n, usage, fb.default_limits()) == (n, "")
    after, why = fb.apply_budget(steps, "croot", [], [], fb.default_limits(), now=NOW)
    assert why == "" and after is steps
    before_kids = fo.child_goals("goal", steps, parent_task_id="t1")
    after_kids = fo.child_goals("goal", after, parent_task_id="t1")
    assert after_kids == before_kids and len(after_kids) == n


def test_the_defaults_leave_room_for_the_largest_split_plus_its_merge():
    assert fb.DEFAULT_MAX_TOTAL >= fo.MAX_CHILDREN + fb.MERGE_RESERVE + 1
    assert fo.MAX_DEPTH == 1          # the premise: a root only ever splits once


# ---------------------------------------------------------------- the worker's split path

SPLIT = "".join("%d. slice number %d of the whole job\n" % (i, i) for i in range(1, 6)) \
    + "\n" + fo.SUBTASKS_READY


def _worker(spawn):
    return rf.RelayWorker({"text": "1〜5月のメールを一覧化する", "depth": 0}, "w0",
                          fanout=True, spawn_fn=spawn)


def _spawner(grant=None):
    got = []

    def spawn(goal, kids, parent_checks=None, parent_partial=""):
        got.append(kids)

    if grant is not None:
        spawn.grant = grant
    return spawn, got


def test_without_a_budget_the_worker_splits_as_before():
    spawn, got = _spawner()
    w = _worker(spawn)
    w._decide(SPLIT)
    assert len(got) == 1 and len(got[0]) == 5 and w.outcome == "FANOUT"


def test_with_the_default_budget_the_worker_splits_identically():
    base, got0 = _spawner()
    budgeted, got1 = _spawner(lambda goal, steps, tid="": fb.apply_budget(
        steps, "root", [], [], fb.default_limits(), min_children=fo.MIN_CHILDREN))
    w0, w1 = _worker(base), _worker(budgeted)
    w0._decide(SPLIT)
    w1._decide(SPLIT)
    assert [k["text"] for k in got1[0]] == [k["text"] for k in got0[0]]
    assert (w1.status, w1.outcome) == (w0.status, w0.outcome)


def test_a_refused_grant_runs_the_goal_directly_and_is_not_stuck():
    spawn, got = _spawner(lambda goal, steps, tid="": ([], "tree has 24 workers (limit 24)"))
    w = _worker(spawn)
    w._decide(SPLIT)
    assert got == []
    assert w.status == "ready" and w.outcome != "STUCK" and w.fanout is False
    assert "予算" in w.reason and "24" in w.reason
    assert "直接実行" in w.job


def test_a_partial_grant_trims_the_children_without_dropping_a_slice():
    spawn, got = _spawner(lambda goal, steps, tid="": (fb.trim_steps(steps, 3), "partial"))
    w = _worker(spawn)
    w._decide(SPLIT)
    kids = got[0]
    assert len(kids) == 3 and w.outcome == "FANOUT"
    assert "slice number 5" in kids[2]["text"] and "slice number 3" in kids[2]["text"]


# ---------------------------------------------------------------- status export

def test_the_snapshot_exports_the_limits_and_per_root_usage(monkeypatch):
    monkeypatch.setattr(FR, "_campaign_lines", lambda: [
        '{"kind": "campaign", "campaign_id": "r1", "root_id": "r1", "n": 2, "ts": 1.0}'])
    rows = [_row("r1", "working", 3), _row("r1", "done", 2), {"status": "done", "turn": 1}]
    blk = FR._tree_budget_block(rows)
    assert blk["fanout_budget"] == fb.limits_from_settings()
    t = blk["tree_budget"]["r1"]
    assert (t["total"], t["active"], t["turns"]) == (2, 1, 5)
    assert t["limits"] == blk["fanout_budget"]


def test_the_export_without_any_tree_still_reports_the_limits():
    blk = FR._tree_budget_block([{"status": "done", "turn": 1}])
    assert blk["tree_budget"] == {} and set(blk["fanout_budget"]) == {"total", "active", "turns", "wall_min"}


def test_the_export_is_capped():
    rows = [_row("r%03d" % i) for i in range(fb.MAX_ROOTS_EXPORTED + 15)]
    blk = fb.status_block(rows, [], dict(LIM), now=NOW)
    assert len(blk["tree_budget"]) == fb.MAX_ROOTS_EXPORTED
    assert fb.status_block(None, [], dict(LIM)) == {}


def test_a_snapshot_failure_omits_the_budget_rather_than_the_status(monkeypatch):
    monkeypatch.setattr(fb, "status_block", lambda *a, **k: 1 / 0)
    assert FR._tree_budget_block([_row("r")]) == {}


def test_snapshot_calls_the_exporter():
    src = _read("relay", "fleet_runner.py")
    assert '_snap.update(_tree_budget_block(_snap["workers"]))' in src


# ---------------------------------------------------------------- settings

@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.txt"
    monkeypatch.setattr(SP, "NEW_PATH", str(path))

    def write(*lines):
        path.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
    return write


def test_absent_keys_mean_the_registry_defaults(settings):
    settings("dark=0")
    got = fb.limits_from_settings()
    assert got == {"total": SK.default("fanout_max_total"), "active": SK.default("fanout_max_active"),
                   "turns": SK.default("fanout_max_turns"), "wall_min": SK.default("fanout_max_wall_min")}


def test_the_active_limit_follows_maxtabs_when_unset_and_the_key_when_set(settings):
    settings("maxtabs=8")
    assert fb.limits_from_settings()["active"] == 8
    settings("maxtabs=8", "fanout_max_active=2")
    assert fb.limits_from_settings()["active"] == 2


def test_values_are_clamped_and_junk_falls_back(settings):
    settings("fanout_max_total=1", "fanout_max_active=9999", "fanout_max_turns=abc",
             "fanout_max_wall_min=-5")
    got = fb.limits_from_settings()
    assert got["total"] == fb.BOUNDS[fb.KEY_TOTAL][0]
    assert got["active"] == fb.BOUNDS[fb.KEY_ACTIVE][1]
    assert got["turns"] == fb.DEFAULT_MAX_TURNS
    assert got["wall_min"] == fb.BOUNDS[fb.KEY_WALL][0]


def test_the_keys_are_declared_each_gate_with_the_python_defaults():
    for key, val in ((fb.KEY_TOTAL, fb.DEFAULT_MAX_TOTAL), (fb.KEY_ACTIVE, fb.DEFAULT_MAX_ACTIVE),
                     (fb.KEY_TURNS, fb.DEFAULT_MAX_TURNS), (fb.KEY_WALL, fb.DEFAULT_MAX_WALL_MIN)):
        assert SK.effect(key) == SK.EACH_GATE and SK.default(key) == val


def test_the_active_default_is_the_fleets_own_concurrency_cap():
    assert fb.DEFAULT_MAX_ACTIVE == FR.DEFAULT_MAX_CONCURRENT == SK.default("maxtabs")


def test_the_terminal_statuses_match_the_fleets():
    assert tuple(fb._TERMINAL) == tuple(rf.TERMINAL)
    assert rf.PENDING in fb._NOT_YET_RUNNING


# ---------------------------------------------------------------- the cockpit (source + parity)

def _cs_array(src, name):
    m = re.search(r"public static readonly (?:int|string)\[\] %s\s*=\s*\{([^}]*)\}" % name, src, re.S)
    assert m, name
    return [x.strip().strip('"') for x in m.group(1).split(",") if x.strip()]


def test_the_cockpit_keys_defaults_and_bounds_equal_the_python_side():
    src = _read("ui", "EffortPolicy.cs")
    keys = [fb.KEY_TOTAL, fb.KEY_ACTIVE, fb.KEY_TURNS, fb.KEY_WALL]
    assert _cs_array(src, "Keys") == keys
    assert [int(x) for x in _cs_array(src, "Defaults")] == [SK.default(k) for k in keys]
    assert [int(x) for x in _cs_array(src, "Lows")] == [fb.BOUNDS[k][0] for k in keys]
    assert [int(x) for x in _cs_array(src, "Highs")] == [fb.BOUNDS[k][1] for k in keys]
    assert _cs_array(src, "ReportKeys") == ["total", "active", "turns", "wall_min"]


def test_the_shipped_view_has_no_wpf():
    src = _read("ui", "EffortPolicy.cs")
    assert "System.Windows" not in src and "PresentationFramework" not in src


def test_the_cockpit_has_the_four_boxes_persisted_through_savekey_only():
    src = _read("ui", "FleetCockpit.cs")
    # lives in the gear popup (BuildSettingsPanel), NOT in the header
    assert "col.Children.Add(FanoutBudgetControl());" in src
    assert "ctrls.Children.Add(FanoutBudgetControl());" not in src
    for key in ("fanout_max_total", "fanout_max_active", "fanout_max_turns", "fanout_max_wall_min"):
        assert len(re.findall(r'SaveKey\("%s", s\)' % key, src)) == 1, key
        assert re.search(r'case "%s":' % key, src), key           # the timing switch names it
    # the timing switch says each_gate for all four (and nowhere else)
    m = re.search(r'case "fanout_max_total":\s*case "fanout_max_active":\s*case "fanout_max_turns":'
                  r'\s*case "fanout_max_wall_min":\s*case "effort_policy":\s*return "each_gate";', src)
    assert m
    # the boxes write nothing else: no direct file or constant access from the control
    body = re.search(r"UIElement FanoutBudgetControl\(\).*?\n    }\n", src, re.S).group(0)
    assert "File." not in body and "WriteAll" not in body


def test_the_cockpit_loads_with_validation_and_does_not_refire():
    src = _read("ui", "FleetCockpit.cs")
    assert "FanoutBudgetView.ParseLine(fbi, ln)" in src                 # settings load, clamped
    assert "FanoutBudgetView.TryParseInput(idx, tb.Text, out v)" in src   # typed value validated
    m = re.search(r"void CommitFanoutBudget\(int idx\).*?\n    }\n", src, re.S)
    assert m and "if (v == _fbVals[idx]) return;" in m.group(0)         # unchanged -> no write
    m = re.search(r"void PaintFanoutBudget\(\).*?\n    }\n", src, re.S)
    assert m and "!tb.IsKeyboardFocused && tb.Text != _fbVals[i].ToString()" in m.group(0)


def test_the_screen_reads_the_runners_report_not_its_own_boxes():
    src = _read("ui", "FleetCockpit.cs")
    m = re.search(r"void PaintFanoutBudgetInEffect\(.*?\n    }\n", src, re.S)
    assert m and 'Obj(root, "fanout_budget")' in m.group(0)
    assert "FanoutBudgetView.Describe(" in m.group(0) and "FanoutBudgetView.PendingText(" in m.group(0)
    assert re.search(r"PaintFanoutInEffect\(root\);.*?PaintFanoutBudgetInEffect\(root\);", src, re.S)


def test_the_status_keys_the_cockpit_reads_are_the_ones_the_runner_writes():
    src = _read("relay", "fanout_budget.py")
    assert '"fanout_budget": dict(lim)' in src and '"tree_budget"' in src


# ---------------------------------------------------------------- the wiring in relay_fleet

def test_the_split_path_asks_the_budget_and_the_coordinator_provides_it():
    src = _read("relay", "relay_fleet.py")
    assert 'getattr(self._spawn_fn, "grant", None)' in src
    assert "_spawn_children.grant = _grant_children" in src
    i = src.index("def _grant_children(")
    body = src[i:src.index("_spawn_children.grant = _grant_children", i)]
    # an already-adopted family queues nothing, so it is not charged
    assert "if cid in campaigns or _campaign_already_on_disk(cid):" in body
    assert "fanout_budget_mod.apply_budget(" in body and "limits_from_settings()" in body
    assert "min_children=fanout_mod.MIN_CHILDREN" in body
    # a failure of the check refuses (fail closed) rather than splitting unbounded
    assert "use, why = [], " in body
