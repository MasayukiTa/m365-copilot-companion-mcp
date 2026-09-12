# -*- coding: utf-8 -*-
"""The refuse-on-landed branch now has a second axis: can the act's effect be checked?

The reconnect budget refused every acting goal whose turn might already have landed, which is
right for an effect nobody here can observe (a mail send) and needlessly lossy for one that
leaves a trace (a git commit in `git log`). These tests pin the split: refuse stays the
default for every un-checkable effect and for a missing/failing checker, and only a checkable
effect with a working checker turns the guess into a look.

PURE. Everything here is `transport_policy`; no repo, no subprocess, no worker. The checker is
a stub, because the decision -- not the I/O -- is what this module owns.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.transport_policy import (  # noqa: E402
    effect_is_checkable,
    resend_decision_for_landed_act,
    CHECK_PRESENT,
    CHECK_ABSENT,
    CHECK_UNKNOWN,
)


# -- effect_is_checkable -------------------------------------------------------------------

def test_a_commit_goal_is_checkable():
    assert effect_is_checkable('commit the fix on the release branch') is True


def test_a_push_goal_is_checkable():
    assert effect_is_checkable('push the merged branch to origin') is True


def test_a_japanese_commit_goal_reflects_the_upstream_act_gate():
    """`effect_is_checkable` gates on `goal_may_act` first, and that acting-predicate does not
    (today) recognise the Japanese コミット request as an act -- so a checkable Japanese effect
    is still routed to refuse. Pinned as-is: this test tracks the real contract, and the day
    `goal_may_act` learns the コミット cue this flips to True with no change here."""
    from relay.transport_policy import goal_may_act
    goal = '修正をコミットしてください'
    assert goal_may_act(goal) is False
    assert effect_is_checkable(goal) is False


def test_a_mail_goal_is_not_checkable():
    """The case the refuse branch exists for: nothing here can read whether mail went."""
    assert effect_is_checkable('send the summary to the team') is False


def test_a_read_only_goal_is_not_checkable():
    """A non-acting goal is not the question this predicate answers -- it never reaches the
    refuse branch -- so it is False, keeping the predicate about acting goals only."""
    assert effect_is_checkable('summarise the open pull requests') is False


def test_english_commit_matches_on_the_keyword_not_the_tense():
    """The English CHECKABLE_EFFECT pattern is a bare `\\bcommit\\b` with no past-tense
    discipline, so a sentence merely MENTIONING a commit is read as checkable once
    `goal_may_act` has passed it. This is safe by construction, not by this predicate: an
    over-broad `checkable=True` only ever routes to `resend_decision_for_landed_act`, which
    still refuses without a checker and only ever re-sends on a present (no-op) or absent
    (genuinely-lost) verdict. Pinned to the real behaviour so a future tightening is a
    deliberate, visible change here."""
    assert effect_is_checkable('the commit landed yesterday, no action needed') is True
    # ...and the decision layer is still safe with no checker:
    assert resend_decision_for_landed_act(
        'the commit landed yesterday, no action needed', checker=None) == 'refuse'


def test_none_and_empty_are_not_checkable():
    assert effect_is_checkable('') is False
    assert effect_is_checkable(None) is False


# -- resend_decision_for_landed_act -- the default is unchanged ----------------------------

def test_no_checker_refuses_even_for_a_checkable_effect():
    """The whole point is safety-by-default: with no way to look, a checkable effect is still
    refused, exactly as the code did before a checker existed."""
    assert resend_decision_for_landed_act('commit the fix', checker=None) == 'refuse'


def test_an_uncheckable_effect_refuses_even_with_a_checker():
    """A checker for the wrong kind of effect must not unlock a re-send: mail is refused no
    matter what a checker says, because the predicate gates on the effect being checkable."""
    calls = []

    def checker(goal):
        calls.append(goal)
        return CHECK_ABSENT

    assert resend_decision_for_landed_act('send the report', checker=checker) == 'refuse'
    assert calls == [], 'the checker must not even be consulted for an un-checkable effect'


# -- resend_decision_for_landed_act -- the check replaces the guess ------------------------

def test_present_effect_resends_as_a_no_op():
    """A commit already in the log means a re-send changes nothing -- safe, and preferred over
    ending the worker on a transient wobble."""
    assert resend_decision_for_landed_act(
        'commit the fix', checker=lambda g: CHECK_PRESENT) == 'resend'


def test_absent_effect_resends_as_the_recovery():
    """A commit the log does NOT show did not land -- the turn is genuinely lost and re-sending
    is the recovery the reconnect budget was always for."""
    assert resend_decision_for_landed_act(
        'commit the fix', checker=lambda g: CHECK_ABSENT) == 'resend'


def test_unknown_verdict_falls_back_to_refuse():
    assert resend_decision_for_landed_act(
        'commit the fix', checker=lambda g: CHECK_UNKNOWN) == 'refuse'


def test_a_raising_checker_is_read_as_unknown_not_absent():
    """A checker failing is not evidence the effect is absent -- treating it as absent would
    re-send an act that may have landed, the exact hazard the branch prevents."""
    def checker(goal):
        raise RuntimeError('git not found')

    assert resend_decision_for_landed_act('commit the fix', checker=checker) == 'refuse'


def test_the_checker_is_handed_the_goal():
    seen = []
    resend_decision_for_landed_act('commit \"fix the empty-input case\"',
                                   checker=lambda g: seen.append(g) or CHECK_PRESENT)
    assert seen == ['commit \"fix the empty-input case\"']



# -- worker-side I/O: _commit_subject_from_goal and _effect_checker against real git --------
#
# The decision layer above is pure; these exercise the half that actually reads a repository,
# because "call the function and confirm" means the git-backed path too, not only the stub.
# Linux-safe: only `git init/add/commit/log`, no OS-specific paths.

from relay import relay_fleet as rf  # noqa: E402


def _git(repo, *args):
    from tools.childproc import run as _run_child
    _run_child(["git", "-C", str(repo), *args], check=True)


def _repo_with_commit(tmp_path, subject):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", subject)
    return repo


def _mk_worker(goal, repo):
    w = rf.RelayWorker.__new__(rf.RelayWorker)
    w.goal = goal
    w._effect_repo = str(repo) if repo is not None else None
    return w


def test_commit_subject_is_read_from_the_quoted_text():
    assert rf._commit_subject_from_goal('commit "fix the empty-input case"') \
        == "fix the empty-input case"


def test_commit_subject_is_none_when_nothing_is_quoted():
    """No invented subject: an unquoted goal yields None so the caller refuses, not guesses."""
    assert rf._commit_subject_from_goal("commit the fix") is None
    assert rf._commit_subject_from_goal("") is None
    assert rf._commit_subject_from_goal(None) is None


def test_checker_reports_present_when_the_commit_is_in_the_log(tmp_path):
    repo = _repo_with_commit(tmp_path, "fix the empty-input case")
    w = _mk_worker('commit "fix the empty-input case"', repo)
    checker = w._effect_checker()
    assert checker is not None
    assert checker(w.goal) == "present"


def test_checker_reports_absent_when_the_commit_is_not_in_the_log(tmp_path):
    repo = _repo_with_commit(tmp_path, "some unrelated commit")
    w = _mk_worker('commit "a subject that never happened here"', repo)
    assert w._effect_checker()(w.goal) == "absent"


def test_checker_is_none_without_a_repo_so_the_default_stays_refuse(tmp_path):
    """No repo to look in -> None -> resend_decision_for_landed_act refuses. Safe default."""
    w = _mk_worker('commit "fix the empty-input case"', None)
    assert w._effect_checker() is None


def test_checker_is_none_for_an_uncheckable_effect_even_with_a_repo(tmp_path):
    repo = _repo_with_commit(tmp_path, "anything")
    w = _mk_worker("send the report", repo)
    assert w._effect_checker() is None


def test_checker_reports_unknown_when_the_goal_names_no_subject(tmp_path):
    """Checkable effect, real repo, but nothing quoted -> unknown -> the decision refuses."""
    repo = _repo_with_commit(tmp_path, "anything")
    w = _mk_worker("commit the fix", repo)
    assert w._effect_checker()(w.goal) == "unknown"


def test_end_to_end_present_commit_yields_resend(tmp_path):
    """The whole path: checkable effect + repo where the commit exists -> resend (a no-op)."""
    from relay.transport_policy import resend_decision_for_landed_act
    repo = _repo_with_commit(tmp_path, "fix the empty-input case")
    w = _mk_worker('commit "fix the empty-input case"', repo)
    assert resend_decision_for_landed_act(w.goal, checker=w._effect_checker()) == "resend"


def test_end_to_end_absent_commit_yields_resend(tmp_path):
    from relay.transport_policy import resend_decision_for_landed_act
    repo = _repo_with_commit(tmp_path, "unrelated")
    w = _mk_worker('commit "never happened"', repo)
    assert resend_decision_for_landed_act(w.goal, checker=w._effect_checker()) == "resend"


def test_end_to_end_mail_goal_yields_refuse(tmp_path):
    """The default the branch exists for is untouched: a mail send still refuses."""
    from relay.transport_policy import resend_decision_for_landed_act
    repo = _repo_with_commit(tmp_path, "anything")
    w = _mk_worker("send the report to the team", repo)
    assert resend_decision_for_landed_act(w.goal, checker=w._effect_checker()) == "refuse"


def test_checker_falls_back_to_the_goal_cwd_when_no_effect_repo_is_set(tmp_path):
    """The repo the commit lands in is the goal's own working directory. `_effect_repo` is an
    override; when it is unset the checker looks in `self.cwd`, so the feature actually fires
    for a normal goal (which carries cwd, not _effect_repo)."""
    repo = _repo_with_commit(tmp_path, "fix the empty-input case")
    w = rf.RelayWorker.__new__(rf.RelayWorker)
    w.goal = 'commit "fix the empty-input case"'
    w.cwd = str(repo)          # the ordinary worker attribute, no _effect_repo
    checker = w._effect_checker()
    assert checker is not None
    assert checker(w.goal) == "present"


def test_effect_repo_overrides_cwd_when_both_are_set(tmp_path):
    """An explicit `_effect_repo` wins over cwd, so a fleet that knows a different tree can say
    so. Here cwd has the commit and _effect_repo does not, and the override's absent verdict
    is what surfaces."""
    with_commit = _repo_with_commit(tmp_path, "the real subject")
    without = tmp_path / "other"
    without.mkdir()
    _git(without, "init", "-q")
    _git(without, "config", "user.email", "t@example.com")
    _git(without, "config", "user.name", "t")
    (without / "b.txt").write_text("y", encoding="utf-8")
    _git(without, "add", "b.txt")
    _git(without, "commit", "-q", "-m", "unrelated")
    w = rf.RelayWorker.__new__(rf.RelayWorker)
    w.goal = 'commit "the real subject"'
    w.cwd = str(with_commit)
    w._effect_repo = str(without)
    assert w._effect_checker()(w.goal) == "absent"


def test_checker_is_none_when_neither_repo_nor_cwd_is_set(tmp_path):
    """Nowhere to look -> None -> the decision refuses. The safe default survives a worker with
    no working directory at all."""
    w = rf.RelayWorker.__new__(rf.RelayWorker)
    w.goal = 'commit "fix the empty-input case"'
    w.cwd = None
    assert w._effect_checker() is None
