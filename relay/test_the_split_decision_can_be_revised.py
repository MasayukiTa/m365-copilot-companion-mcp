# -*- coding: utf-8 -*-
"""The split decision was made at the one moment nothing was known, and could never change.

THREE THINGS WERE WRONG WITH IT, and they compound.

(1) IT WAS ASKED FOR NOBODY. `--fanout` was off by default and its own help text carried the
reason -- "a goal that fits should not pay for a split turn and a merge turn". True while the
flag WAS the decision; false since the per-goal judge went in, because
`self.fanout = bool(fanout) and _depth0 and _goal_splittable` means a goal that fits is judged
NO_SPLIT and pays nothing. The flag now only decides whether the question is ever asked, and
off-by-default meant it was asked for nobody who did not know to opt in.

(2) AN OFFLINE RULESET SETTLED WHAT IT SAYS IT CANNOT SETTLE. `splittability` is explicit --
"It makes NO live model call" -- and returns UNCERTAIN for "not enough signal either way".
`should_split` is `decision == SPLIT`, so every UNCERTAIN resolved silently to "no". The
regexes were answering a question they document themselves as unable to answer.

(3) THE PROMPT PERMITTED ONLY ONE VERDICT. It demanded 2〜12 subtasks with no way to say "this
is one indivisible investigation", so an agent handed something unsplittable had to invent a
split or stall. A judge with one permitted answer is not a judge. `NO_SPLIT` is the other one.

AND THE DECISION WAS FROZEN AT TURN 1. `SPLIT_JOB` is pre-loaded in __init__ and the branch is
gated on `self.fanout and not self._fanout_done`, so a goal that turns out mid-run not to fit
had no way to say so. `max_continue` (6) already counts exactly that: six replies that each
report progress and never finish -- not repetition, which is `no_progress`, a separate counter.
The worker's answer to that evidence was to die STUCK. It asks for a split now, once, at depth
0, and carries the work it had already finished into the campaign so the rescue does not
destroy what it was rescuing.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout as fo              # noqa: E402
from relay import splittability as sp       # noqa: E402
from relay import relay_fleet as rf         # noqa: E402
from relay import task_router as tr         # noqa: E402


# ── (1) the capability is on ──────────────────────────────────────────────────────────────

def test_a_run_is_fanout_capable_without_anyone_asking():
    assert tr._wants_fanout([{"text": "短い用事"}]) is True
    assert tr._wants_fanout([]) is True


def test_an_operator_can_still_turn_the_capability_off(monkeypatch):
    """The env override is the only way to answer 'no' for a whole run -- and it is the only
    answer that cannot be revisited, since it takes the question away from the per-goal judge
    for goals nobody has seen yet."""
    monkeypatch.setenv("FLEET_INTAKE_AUTOSTART_FANOUT", "0")
    assert tr._wants_fanout([{"text": "x" * 5000}]) is False


def test_the_runner_flag_defaults_on_and_can_be_negated():
    import argparse
    import relay.fleet_runner as fr
    src = open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8").read()
    assert "BooleanOptionalAction" in src and "--fanout" in src
    assert hasattr(argparse, "BooleanOptionalAction"), "argparse is too old for --no-fanout"
    del fr


def test_the_library_entry_points_default_on():
    import inspect
    for fn in (rf.RelayWorker.__init__, rf.run_relay_fleet):
        assert inspect.signature(fn).parameters["fanout"].default is True, (
            "%s still defaults fan-out off" % fn.__qualname__)


# ── (2) UNCERTAIN reaches the model ───────────────────────────────────────────────────────

def test_the_offline_judge_still_refuses_to_answer_uncertain():
    """The premise. If this ever changes, the branch below is answering a question that was
    already settled, and this file should be re-derived rather than patched."""
    assert sp.Verdict(sp.UNCERTAIN, "no signal").should_split is False


def _eligible(goal, decision):
    """What __init__ decides for a goal whose offline triage returned `decision`."""
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    assert "_d == _splittability.UNCERTAIN" in src, (
        "UNCERTAIN is resolved without asking anyone")
    v = sp.Verdict(decision, "r")
    return bool(v.should_split) or decision == sp.UNCERTAIN


def test_uncertain_is_asked_rather_than_assumed():
    assert _eligible("g", sp.UNCERTAIN) is True
    assert _eligible("g", sp.SPLIT) is True
    assert _eligible("g", sp.NO_SPLIT) is False, (
        "a confident NO_SPLIT should not cost a turn -- that is what keeps default-on free")


# ── (3) the agent may decline ─────────────────────────────────────────────────────────────

def test_the_prompt_offers_the_agent_a_way_to_say_no():
    assert fo.NO_SPLIT_MARKER in fo.SPLIT_JOB
    assert fo.SUBTASKS_READY in fo.SPLIT_JOB


def test_a_decline_is_read_as_a_decline():
    assert fo.declined_split("これは1つの調査で分割できません。\nNO_SPLIT") is True
    assert fo.declined_split("1. A\n2. B\nSUBTASKS_READY") is False


def test_a_reply_naming_both_markers_is_decided_by_the_last_line():
    """The prompt names both, so an agent explaining its choice quotes both. Whichever it
    ENDED on is the answer; read the other way round a decline becomes an empty subtask list,
    which is handled as a malformed split -- the same work, the opposite meaning in the
    record, and a deliberate refusal counted as a parse failure."""
    assert fo.declined_split(
        "NO_SPLIT と SUBTASKS_READY のうち、分割はしません。\nNO_SPLIT") is True
    assert fo.declined_split(
        "NO_SPLIT も検討しましたが分割します。\n1. A\n2. B\nSUBTASKS_READY") is False


def test_a_reply_with_neither_marker_is_not_a_decline():
    assert fo.declined_split("作業中です") is False
    assert fo.declined_split("") is False


def test_the_worker_has_a_branch_for_the_decline():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("if fanout_mod.declined_split(resp):")
    j = src.index("if fanout_mod.fanout_ready(resp):", i)
    branch = src[i:j]
    assert "self.fanout = False" in branch and "self.status = \"ready\"" in branch
    assert "fanout_declined" in branch, "a deliberate refusal leaves no record"
    assert i < j, "the decline must be tested before fanout_ready, or it never matches"


# ── (4) the decision can be revised mid-run ───────────────────────────────────────────────

def _worker(goal="長い作業", fanout=True, depth=0, spawn=lambda *a, **k: None):
    return rf.RelayWorker({"text": goal, "depth": depth}, "w0", fanout=fanout, spawn_fn=spawn)


def test_a_long_running_goal_is_offered_a_split_instead_of_dying():
    w = _worker()
    w._fanout_capable = True
    w._fanout_done = True          # turn 1 already decided not to split
    w._midrun_split_asked = False
    w._fanout_done = False
    assert w._ask_for_a_midrun_split() is True
    assert w.fanout is True and w._fanout_done is False
    assert w.status == "ready"
    assert str(w.max_continue) in w.job, "the evidence is not in the request"


def test_the_offer_is_made_once():
    w = _worker()
    w._fanout_capable = True
    assert w._ask_for_a_midrun_split() is True
    assert w._ask_for_a_midrun_split() is False, (
        "asking again spends the remaining turns on the question instead of the work")


def test_a_child_is_never_offered_a_mid_run_split():
    """A child that splits makes grandchildren, which MAX_DEPTH forbids."""
    w = _worker(depth=1)
    assert w._fanout_capable is False
    assert w._ask_for_a_midrun_split() is False


def test_a_run_that_is_not_fanout_capable_is_not_offered_one():
    w = _worker(fanout=False)
    assert w._ask_for_a_midrun_split() is False


def test_a_worker_with_nowhere_to_put_children_is_not_offered_one():
    """No spawn_fn means the children would be built and dropped."""
    w = _worker(spawn=None)
    assert w._fanout_capable is False
    assert w._ask_for_a_midrun_split() is False


def test_the_continue_cap_tries_the_split_before_giving_up():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("if self._continue_count >= self.max_continue:")
    branch = src[i:src.index("self.job = self._task_anchor(_continue_nudge", i)]
    assert "_ask_for_a_midrun_split()" in branch
    assert branch.index("_ask_for_a_midrun_split()") < branch.index('"stuck", "STUCK"'), (
        "the split is offered after the worker has already been declared stuck")


# ── (5) the rescue does not destroy the work it rescues ───────────────────────────────────

def test_the_pre_split_parents_work_reaches_the_merge():
    recs = [{"subtask_index": 1, "outcome": "DONE", "result": "残り1"},
            {"subtask_index": 2, "outcome": "DONE", "result": "残り2"}]
    item = fo.aggregation_goal("親", recs, parent_partial="1月分 120件を取得済み")
    assert "120件" in item["text"], (
        "分割前に親が終えた作業が統合に届いていない -- 救済のはずが破棄になっている")


def test_the_pre_split_parent_is_not_presented_as_a_finished_slice():
    """それは**完了していない**。完了していないから分割された。

    一度これを `{"subtask_index": 0, "outcome": "DONE"}` として記録した。ギャップ検査を
    動かさずに済むからで、つまり都合のよい虚偽を選んだ -- 「欠落を書かなかったせいで完全に
    読める報告」を止めるためだけに存在する仕組みの中で。材料として渡し、未検証と明記し、
    スライスとしては数えない。
    """
    recs = [{"subtask_index": 1, "outcome": "DONE", "result": "a"}]
    item = fo.aggregation_goal("親", recs, parent_partial="途中まで")
    assert "未検証" in item["text"], "途中経過が完了扱いで提示されている"
    assert fo.missing_slices(recs) == [], "前提が崩れている"


def test_a_turn_one_split_has_no_partial_and_is_unchanged():
    recs = [{"subtask_index": 1, "outcome": "DONE", "result": "a"}]
    assert fo.aggregation_goal("親", recs) == fo.aggregation_goal("親", recs, parent_partial="")


def test_the_partial_does_not_move_the_gap_check():
    """It is recorded DONE at slice 0, so it adds material without making the sweep look more
    complete than it is."""
    recs = [{"subtask_index": 1, "outcome": "DONE", "result": "a"},
            {"subtask_index": 2, "outcome": "STUCK", "result": ""}]
    item = fo.aggregation_goal("親", recs, parent_partial="先に終わった分")
    gaps = [c for c in item["checks"] if c.get("all_of")]
    assert gaps and gaps[0]["all_of"] == ["2"]


def test_the_partial_survives_a_crash():
    header = json.dumps({"kind": "campaign", "campaign_id": "cZ", "goal": "親", "n": 2,
                         "cwd": None, "checks": [], "partial": "先に終わった分"},
                        ensure_ascii=False)
    fam = fo.campaigns_from_ledger([header])
    assert fam["cZ"]["partial"] == "先に終わった分", (
        "クラッシュ後に再構成すると親の完了分が永久に失われる")


def test_an_old_header_without_the_field_rebuilds_empty():
    header = json.dumps({"kind": "campaign", "campaign_id": "cW", "goal": "親", "n": 2},
                        ensure_ascii=False)
    assert fo.campaigns_from_ledger([header])["cW"]["partial"] == ""
