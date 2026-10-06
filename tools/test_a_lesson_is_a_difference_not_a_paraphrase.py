# -*- coding: utf-8 -*-
"""The lesson extractor must quote what changed, and must not invent or launder.

Three failure modes are pinned here, each of which was observed while building the thing:

1. GROUPING BY WORDING CANNOT SEE A WORDING CHANGE. The first version grouped attempts using
   `skill_candidates.normalise`, which rewrites paths to `<path>` -- so the instruction that
   named a path and the one that did not fell into different groups, and the extractor returned
   0 lessons from a ledger that contains them. The test holds the fixed behaviour: a pair whose
   only difference is an added path must be found.

2. OUR OWN BOILERPLATE IS NOT A LESSON. Run unfiltered, the top three "recurring lessons" were
   all text `relay/fanout.py` and `relay/project_memory.py` write into goals themselves. A
   generator that mines its own output reports the machine's habits back as discoveries.

3. A REPHRASING IS NOT AN ADDITION. If the same requirement said differently counted as new,
   every draft would fill with noise no operator ever chose to add.

The fixtures are synthetic. Real goal text is business content living under git-ignored .fleet,
and copying it into a tracked test is the transcription this repository has broken three times.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import skill_lessons  # noqa: E402


def _ledger(tmp_path, rows):
    path = tmp_path / "ledger.jsonl"
    with io.open(str(path), "w", encoding="utf-8") as fh:
        for row in rows:
            row = dict(row)
            row.setdefault("event", "worker_done")
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return str(path)


def _row(goal, outcome, ts=1.0):
    return {"goal": goal, "outcome": outcome, "ts": ts}


BASE = ("collect the quarterly coating inspection figures and report them as a table "
        "with one row per production lot and a total line at the bottom")


def test_an_added_path_is_the_lesson_that_grouping_by_wording_could_never_see(tmp_path):
    # The shape the OGF experiment produced: identical work, and the only stated difference is
    # that the finishing instruction said WHERE the files were.
    led = _ledger(tmp_path, [
        _row(BASE + ".", "STUCK", 1.0),
        _row(BASE + ". the source workbooks are under D:/shared/quarterly/inputs.", "DONE", 2.0),
    ])
    found = skill_lessons.pairs(ledger=led)
    assert len(found) == 1, found
    joined = " ".join(found[0]["added"])
    assert "D:/shared/quarterly/inputs" in joined


def test_the_same_requirement_reordered_is_not_an_addition(tmp_path):
    # Same content words, different arrangement. The rule the module actually claims is "brings
    # vocabulary the failing instruction never used" -- so a reordering is silent, while an
    # inflected rewording ("report" -> "reported") WOULD be reported as an addition. That limit
    # is stated in `added.__doc__` rather than tested away here: a draft is read by a person,
    # and a noisy line they discard costs less than a real lesson stemmed into invisibility.
    led = _ledger(tmp_path, [
        _row(BASE + ". report them as a table with a total line at the bottom.", "STUCK", 1.0),
        _row(BASE + ". at the bottom a total line. as a table, report them.", "DONE", 2.0),
    ])
    assert skill_lessons.pairs(ledger=led) == []


def test_identical_instructions_carry_no_lesson_however_the_runs_ended(tmp_path):
    # Same text, one stuck and one done: the difference was in the world, not the instruction,
    # and a Skill written from it would teach nothing.
    led = _ledger(tmp_path, [_row(BASE + ".", "STUCK", 1.0), _row(BASE + ".", "DONE", 2.0)])
    assert skill_lessons.pairs(ledger=led) == []


def test_unrelated_work_that_merely_shares_a_vocabulary_is_not_the_same_work(tmp_path):
    led = _ledger(tmp_path, [
        _row("collect the quarterly coating inspection figures for the audit binder.",
             "STUCK", 1.0),
        _row("draft the quarterly safety briefing slides for the morning meeting and "
             "circulate them to the shift leads before friday.", "DONE", 2.0),
    ])
    assert skill_lessons.pairs(ledger=led) == []


def test_one_failure_yields_one_comparison_not_one_per_similar_success(tmp_path):
    # Twenty near-identical successes around one stuck run used to produce twenty "pairs",
    # inflating every count downstream while carrying a single fact.
    rows = [_row(BASE + ".", "STUCK", 1.0)]
    for n in range(20):
        rows.append(_row(BASE + ". the source workbooks are under D:/shared/quarterly/inputs, "
                         "sheet %d." % n, "DONE", 2.0 + n))
    found = skill_lessons.pairs(ledger=_ledger(tmp_path, rows))
    assert len(found) == 1, "%d comparisons for one failure" % len(found)


def test_a_lesson_is_not_our_own_boilerplate(tmp_path):
    # Both halves matter. The filter must drop the machine's text...
    injected = skill_lessons.MACHINE_AUTHORED[0][0]
    led = _ledger(tmp_path, [
        _row(BASE + ".", "STUCK", 1.0),
        _row(BASE + "。【" + injected + " — 全体の 1/3】", "DONE", 2.0),
    ])
    assert skill_lessons.pairs(ledger=led) == []

    # ...and it must still match the module that emits it. If the fan-out wording is reworded,
    # this goes red -- rather than the filter quietly ceasing to match while the machine's own
    # phrases drift back to the top of the lesson list.
    for mark, source in skill_lessons.MACHINE_AUTHORED:
        text = io.open(os.path.join(REPO, source), encoding="utf-8", errors="replace").read()
        assert mark in text, "%s no longer contains %r -- reword the filter with it" % (
            source, mark)


def test_a_benchmark_is_not_the_operators_work(tmp_path):
    led = _ledger(tmp_path, [
        _row("fix the failing test in the sympy repository checkout.", "STUCK", 1.0),
        _row("fix the failing test in the sympy repository checkout, the tree is at "
             "D:/work/sympy.", "DONE", 2.0),
    ])
    assert skill_lessons.pairs(ledger=led) == []
    assert len(skill_lessons.pairs(ledger=led, include_benchmarks=True)) == 1


def test_a_recurring_addition_counts_jobs_not_repetitions(tmp_path):
    # The distinction the evidence rests on: the same advice added to two DIFFERENT jobs is
    # evidence; the same job retried twice with the same advice is one observation.
    other = ("reconcile the monthly supplier invoices against the delivery notes and list "
             "every mismatch with its document number")
    advice = "use your own built-in document search rather than the gateway tools."
    led = _ledger(tmp_path, [
        _row(BASE + ".", "STUCK", 1.0),
        _row(BASE + ". " + advice, "DONE", 2.0),
        _row(other + ".", "STUCK", 3.0),
        _row(other + ". " + advice, "DONE", 4.0),
    ])
    found = skill_lessons.pairs(ledger=led)
    assert len(found) == 2, found
    rec = skill_lessons.recurring(found)
    assert rec, "an addition made to two different jobs did not recur"
    assert rec[0]["jobs"] == 2
    assert "built-in document search" in rec[0]["text"]


def test_a_missing_ledger_is_empty_not_an_exception(tmp_path):
    assert skill_lessons.pairs(ledger=str(tmp_path / "nope.jsonl")) == []


@pytest.mark.parametrize("outcome", list(skill_lessons.FAILED))
def test_every_outcome_the_module_calls_a_failure_can_start_a_pair(tmp_path, outcome):
    led = _ledger(tmp_path, [
        _row(BASE + ".", outcome, 1.0),
        _row(BASE + ". the source workbooks are under D:/shared/quarterly/inputs.", "DONE", 2.0),
    ])
    assert len(skill_lessons.pairs(ledger=led)) == 1
