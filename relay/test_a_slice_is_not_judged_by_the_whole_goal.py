# -*- coding: utf-8 -*-
"""A child's completion condition was a question about work it was forbidden to do.

TWO DEFECTS, ONE CALL SITE, both measured 2026-09-13.

(1) EVERY CHILD CARRIED THE PARENT'S WHOLE-GOAL CHECK. Splitting a goal gated on
`{"type": "pytest", "args": "-q tests/"}` three ways produced three children with that exact
check -- while the prompt `child_goals` itself writes tells each of them
「他の範囲は別の会話が並行して担当しているので、手を出さないこと」.

It fails in both directions, depending on the check:

  STRICT -> never passes. Child 1 finishes its slice, the suite still fails on slices 2 and 3,
  the gate reports VERIFY_FAILED, and the child spends its remaining turns being pushed at
  work it was told not to touch.

  LOOSE -> passes for free. `file_exists: out/report.csv` becomes true the moment ANY sibling
  writes it, so a child that did nothing verifies -- and `_salvage_via_checks` promotes that
  into a salvaged DONE at exhaustion.

There is no correct per-slice check to derive: a step is a sentence, and turning it into a
shell command would be a guess. What is correct is where the parent's check belongs. The merge
IS the parent goal finishing, in the parent's cwd, so the check travels to it by the route
`cwd` already takes -- campaign record, ledger header, aggregation_goal.

(2) THE MERGE'S OWN GATE WAS INERT. `merge_acceptance_checks` returned bare strings and
`normalize_checks` "silently drops non-dict members", so:

    aggregation_goal(...)["checks"] -> ['未取得または未完了のサブタスク 2 について …']
    goal_fields(...)                -> []

The worker then took `if not self.checks` -- "no checks -> DONE accepted as before
(back-compat trust)". The one gate standing between a merge and a confident report of an
incomplete sweep had never run. Four tests in test_merge_delivery.py asserted the goal CARRIED
the checks; none asked whether anything READ them.

The recorded incident is two merges that ended DONE having written 「欠落なし」 with slices
missing -- a WRONG sentence, not a missing one, which no positive check can catch.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout as fo                    # noqa: E402
from relay.acceptance import Check, normalize_checks  # noqa: E402
from relay.relay_fleet import goal_fields         # noqa: E402

PARENT = "このリポジトリの全テストを通し、失敗をゼロにする"
PARENT_CHECKS = [{"type": "pytest", "args": "-q tests/"}]
STEPS = ["tests/a 以下を通す", "tests/b 以下を通す", "tests/c 以下を通す"]


# ── (1) the slice is not judged by the whole goal ─────────────────────────────────────────

def test_no_child_carries_the_parents_whole_goal_check():
    kids = fo.child_goals(PARENT, STEPS, cwd="C:/repo")
    for k in kids:
        _text, checks, _cwd = goal_fields(k)
        assert checks == [], (
            "スライス %s が全体目標の検査を背負っている: %r" % (k["subtask_index"], checks))


def test_the_shared_check_cannot_be_reintroduced_by_a_keyword():
    """GONE, NOT IGNORED. A caller that still holds a whole-goal check has to be made to say
    where it goes; silently dropping it would leave that caller believing its children are
    still verified, which is the state this was in."""
    with pytest.raises(TypeError):
        fo.child_goals(PARENT, STEPS, checks=PARENT_CHECKS)


def test_the_cwd_is_still_inherited():
    """Only the check moved. Children share the parent's tree on purpose."""
    kids = fo.child_goals(PARENT, STEPS, cwd="C:/repo")
    assert [k["cwd"] for k in kids] == ["C:/repo"] * 3


def test_the_parents_check_arrives_at_the_merge():
    recs = [{"subtask_index": i, "outcome": "DONE", "result": "ok"} for i in (1, 2, 3)]
    item = fo.aggregation_goal(PARENT, recs, cwd="C:/repo", parent_checks=PARENT_CHECKS)
    _t, checks, cwd = goal_fields(item)
    assert {"type": "pytest", "args": "-q tests/"} in checks, (
        "全体目標の検査がどのワーカーにも届いていない -- 子から外しただけになっている")
    assert cwd == "C:/repo", "検査は親の木で走らなければ意味がない"


def test_a_merge_with_no_parent_check_is_unchanged():
    recs = [{"subtask_index": 1, "outcome": "DONE", "result": "ok"}]
    assert "checks" not in fo.aggregation_goal(PARENT, recs)


def test_the_ledger_carries_the_parents_check_across_a_crash():
    """The ledger exists for the run that dies after splitting. A family rebuilt from it
    without the parent's check merges with nothing verifying it -- silently, and only on the
    crash path, which is the hardest place to notice anything."""
    header = json.dumps({"kind": "campaign", "campaign_id": "cX", "goal": PARENT, "n": 3,
                         "cwd": "C:/repo", "checks": PARENT_CHECKS}, ensure_ascii=False)
    fam = fo.campaigns_from_ledger([header])
    assert fam["cX"]["checks"] == PARENT_CHECKS


def test_an_old_header_without_the_field_still_rebuilds():
    """Every header written before today has no `checks` key. It must rebuild as a family
    with no parent check, not vanish."""
    header = json.dumps({"kind": "campaign", "campaign_id": "cY", "goal": PARENT, "n": 2,
                         "cwd": "C:/repo"}, ensure_ascii=False)
    fam = fo.campaigns_from_ledger([header])
    assert fam["cY"]["goal"] == PARENT
    assert fam["cY"]["checks"] == []


# ── (2) the merge's gate actually runs ────────────────────────────────────────────────────

def _gate(records):
    item = fo.aggregation_goal(PARENT, records)
    _t, checks, _c = goal_fields(item)
    return checks


GAPPED = [{"subtask_index": 1, "outcome": "DONE", "result": "ok"},
          {"subtask_index": 2, "outcome": "STUCK", "result": ""},
          {"subtask_index": 4, "outcome": "STUCK", "result": ""}]


def test_the_gap_check_survives_normalize_checks():
    """THE DEFECT. Strings were dropped here and the merge fell through to back-compat trust."""
    raw = fo.merge_acceptance_checks(GAPPED)
    assert raw, "欠落があるのに検査が組まれていない"
    assert normalize_checks(raw) == raw, "検査が normalize_checks に捨てられている"
    assert _gate(GAPPED), "統合ワーカーに検査が1つも届いていない"


def test_a_report_that_names_its_gaps_is_accepted():
    reply = "サブタスク1は完了、合計 812 件。サブタスク2と4は未取得です。"
    for spec in _gate(GAPPED):
        passed, detail = Check(spec, reply=reply).start().poll()
        assert passed, "正しい報告が落ちている: %s -- %s" % (spec, detail)


def test_a_report_that_says_there_were_no_gaps_is_refused():
    """The sentence actually observed in the incident."""
    reply = "全サブタスクを統合しました。欠落なし。合計 812 件。"
    verdicts = [Check(s, reply=reply).start().poll()[0] for s in _gate(GAPPED)]
    assert not all(verdicts), "「欠落なし」と書いた統合が受理されている"


def test_the_number_check_alone_would_not_have_caught_it():
    """Why the negative check exists, stated as a measurement rather than an opinion.

    MEASURED: one missing slice, number 2, and a reply that says 「欠落なし」 while reporting
    「合計 812 件」. The gap-number check PASSES -- a bare "2" matches inside "812" -- and only
    the 「欠落なし」 check refuses it.

    So the number check is LENIENT by construction. That is the safe direction: it can pass on
    a coincidence but cannot fail on one, so it never blocks a correct report, and the other
    two carry the strictness. With more gaps the coincidence gets less likely, which is exactly
    why it cannot be the only check.
    """
    one_gap = [{"subtask_index": 1, "outcome": "DONE", "result": "ok"},
               {"subtask_index": 2, "outcome": "STUCK", "result": ""}]
    reply = "全サブタスクを統合しました。欠落なし。合計 812 件。"
    gate = _gate(one_gap)

    numbers = [s for s in gate if s.get("all_of")]
    assert numbers and Check(numbers[0], reply=reply).start().poll()[0] is True, (
        "この計測が崩れている -- 前提を確かめ直すこと")

    forbidden = [s for s in gate if s.get("expect") is False]
    assert forbidden and Check(forbidden[0], reply=reply).start().poll()[0] is False
    assert not all(Check(s, reply=reply).start().poll()[0] for s in gate)


def test_a_complete_sweep_still_has_nothing_to_check():
    """An empty list, not a check that passes trivially: 'checked and clean' and 'nothing to
    check' must not read the same in a log."""
    assert fo.merge_acceptance_checks(
        [{"subtask_index": 1, "outcome": "DONE"}]) == []


def test_a_reply_the_gate_never_saw_is_not_a_pass():
    """FAILURE IS NOT PERMISSION. `_salvage_via_checks` runs the checks with no DONE reply in
    hand; answering 'passed' there would accept the very claim the check exists to test."""
    spec = _gate(GAPPED)[0]
    passed, detail = Check(spec, reply=None).start().poll()
    assert passed is False
    assert "no reply text was captured" in detail


def test_an_empty_needle_list_does_not_pass_vacuously():
    passed, detail = Check({"type": "reply_contains"}, reply="anything").start().poll()
    assert passed is False
    assert "no needle" in detail


def test_reply_contains_is_a_recognised_type():
    """An unknown type resolves to '[acceptance: unknown check type]' -- a FAILED check, which
    would have turned the new merge gate into a permanent VERIFY_FAILED instead of a gate."""
    from relay.acceptance import VALID_TYPES
    assert "reply_contains" in VALID_TYPES
