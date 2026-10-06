# -*- coding: utf-8 -*-
"""Two of the diagnosis's four answers were typed out, and the other two were unreachable.

`relay/review_resilience.py::diagnose_after_fresh_replay` maps (original refused, fresh refused,
fresh succeeded, fresh transient) onto a cause, an action, and a sentence. It had no caller.
relay_fleet wrote two of its answers out as literals instead:

    recovery_cause = "session_state"   recovery_result = "recovered"
    recovery_cause = "task_content"    recovery_result = "needs_decomposition"

`RefusalCause.SESSION_STATE` and `TASK_CONTENT` ARE those strings, so both sites were branches of
the function, transcribed. Which left the other two -- a fresh replay that died on a transient
error, and one whose evidence identifies nothing -- **unreachable**, and those are exactly the
two runs where `recovery_cause` and `recovery_result` stayed EMPTY. The states a person opens the
record to understand were the states the record said nothing about.

Same shape as `transport_policy.duplicate_risk` earlier the same day: the predicate existed, and
the caller inlined the one case it happened to need.

NO STRING A READER ALREADY SEES CHANGED. `recovery_cause` comes from the enum, identical today.
`recovery_result` is mapped explicitly rather than taken from `RecoveryAction.value`, because
"recovered" reads better than "fresh_replay" to whoever opens the cockpit; a record's vocabulary
is not the place to economise.
"""
from __future__ import annotations

import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.relay_fleet as RF  # noqa: E402
from relay.review_resilience import RecoveryAction, RefusalCause  # noqa: E402

SRC = io.open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()


def _diagnosed(**kw):
    w = RF.RelayWorker("ある用事", "w0", fanout=False)
    w._apply_diagnosis(**kw)
    return w


# ── the two answers that were already right must not have moved ───────────────────────────

def test_a_task_refused_twice_still_reads_the_same(monkeypatch, tmp_path):
    """The literal this replaced. A record whose vocabulary changed would make every earlier
    row incomparable with every later one."""
    w = _diagnosed(fresh_was_refusal=True, fresh_succeeded=False, fresh_was_transient_error=False)
    assert w.recovery_cause == "task_content"
    assert w.recovery_result == "needs_decomposition"
    assert "refused in two independent conversations" in w.reason


def test_a_replay_that_worked_still_reads_the_same():
    w = _diagnosed(fresh_was_refusal=False, fresh_succeeded=True, fresh_was_transient_error=False)
    assert w.recovery_cause == "session_state"
    assert w.recovery_result == "recovered"
    assert "succeeded in a fresh conversation" in w.reason


# ── the two that could not happen ─────────────────────────────────────────────────────────

def test_a_replay_killed_by_a_transient_error_now_says_so():
    """THE DEFECT. This branch of the diagnosis had no way to be reached, so the run recorded
    an empty cause -- indistinguishable from a run where nothing was diagnosed at all."""
    w = _diagnosed(fresh_was_refusal=False, fresh_succeeded=False, fresh_was_transient_error=True)
    assert w.recovery_cause == RefusalCause.TRANSIENT.value
    assert w.recovery_result == "retry_transient"
    assert w.reason


def test_evidence_that_identifies_nothing_says_that_too():
    """"I could not tell" is an answer, and an empty field is not it."""
    w = _diagnosed(fresh_was_refusal=False, fresh_succeeded=False,
                   fresh_was_transient_error=False)
    assert w.recovery_cause == RefusalCause.UNKNOWN.value
    assert w.recovery_result == "unresolved"
    assert w.reason


# ── the wiring itself ─────────────────────────────────────────────────────────────────────

def test_every_action_has_a_word_for_the_record():
    """A missing row would fall through to the enum's own value and change the vocabulary of
    one outcome only -- the hardest kind of drift to notice."""
    assert set(RF._RECOVERY_RESULT) == set(RecoveryAction) - {
        RecoveryAction.ALTERNATE_EXECUTOR, RecoveryAction.REDACT_OUTPUT}, RF._RECOVERY_RESULT
    assert all(isinstance(v, str) and v for v in RF._RECOVERY_RESULT.values())


def test_the_causes_are_no_longer_written_by_hand():
    """Both literals are gone from the code. If one comes back it is a fifth answer nobody can
    see beside the other four.

    ASSIGNMENTS, VIA THE AST -- not a substring search. The first version of this test read the
    whole file and failed on `_apply_diagnosis`'s own docstring, which quotes the literals it
    replaced. That is the burn-down's own lesson arriving in its own test: a check that reads
    prose describing a past defect reports the defect as present. Check the behaviour, or at
    least the syntax, not the words.
    """
    import ast

    tree = ast.parse(SRC)
    hand_written = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        for t in node.targets:
            # `= ""` in __init__ is the field coming into existence, not an answer. Only a
            # NON-EMPTY literal is a diagnosis somebody decided without diagnosing.
            if (isinstance(t, ast.Attribute) and t.attr in ("recovery_cause", "recovery_result")
                    and isinstance(node.value.value, str) and node.value.value):
                hand_written.append((node.lineno, t.attr, node.value.value))
    assert not hand_written, "診断の答えを手書きしている: %s" % hand_written
    assert "diagnose_after_fresh_replay(" in SRC


def test_every_settle_path_goes_through_it():
    """The literal appeared TWICE -- the settle path and the refute-then-settle path -- and a
    fix that changed one would have left the other transcribing.

    FOUR CALL SITES NOW (plus the definition), AND THE FOURTH IS THE ONE THAT MATTERED.
    2026-09-19: the three that existed all passed `fresh_was_transient_error=False` as a
    LITERAL, so two of `diagnose_after_fresh_replay`'s four answers -- TRANSIENT and UNKNOWN
    -- could not occur in production at all, and `review_resilience.looks_like_transient_error`
    (written to compute that argument) had no caller anywhere. This file had already found the
    same defect one level up, in the answers; the arguments were still transcribed.
    (That predicate was deleted on 2026-09-22: `_decide` supplies the argument from the
    outcome the settling path already decided, so nothing was left for it to compute.)

    The fourth site is not a fourth settle path. It is `_decide`'s wrapper, which observes the
    transition into a terminal state and covers all EIGHTEEN of the INFRA_STUCK give-ups at
    once. A line at each of those would have been the defect restated: the nineteenth would be
    missed exactly as these were.
    """
    assert SRC.count("_apply_diagnosis(") == 5, SRC.count("_apply_diagnosis(")
    assert "fresh_was_transient_error=transient" in SRC, \
        "the transient argument is a literal again, and two answers just went unreachable"


def test_recording_cannot_fail_the_settle_it_describes(monkeypatch):
    """It runs inside settle. A record that can raise turns a finished run into a lost one."""
    monkeypatch.setattr(RF, "_RECOVERY_RESULT", None)
    w = RF.RelayWorker("ある用事", "w0", fanout=False)
    w._apply_diagnosis(fresh_was_refusal=True, fresh_succeeded=False,
                       fresh_was_transient_error=False)
    assert True  # reaching here is the assertion
