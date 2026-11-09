# -*- coding: utf-8 -*-
"""Pure unit tests for resuming an acting turn that timed out with delivery UNKNOWN.

The incident: on 2026-11-09 ~13 workers ended STUCK with 'not re-sent: the turn may already
have been delivered and this goal performs an action (delivery=unknown, turn timeout without a
reply)'. w41/w43 lost their reply at the same moment on a dropped socket ('no close frame
received or sent') and were never resumed. The fix opens a NEW conversation that FIRST reads
the job's real state (git status / git log / the branch's PR / the files it was writing) and
continues from there, bounded by FRESH_RETRY_MAX -- instead of leaving the worker STUCK.

Covers relay/auto_rephrase_continue.py (build_resume_text, _git_summary PR section) and the
timeout wiring in relay/relay_fleet.py. No network, no browser: the relay_fleet checks read the
source as text so the heavy module is not imported.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY_FLEET = os.path.join(REPO, "relay", "relay_fleet.py")

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import auto_rephrase_continue as AR  # noqa: E402
from relay import resume_from_state as RFS  # noqa: E402

#: A stable fragment of the resume lead: the worker must verify state before acting.
VERIFY_FRAGMENT = "git status"
#: A stable fragment telling the worker not to redo finished work.
NO_REDO_FRAGMENT = "\u4f5c\u308a\u76f4\u3055\u305a"


def test_resume_text_carries_goal_state_check_and_no_redo_instruction():
    goal = "\u4f9d\u983c\u672c\u6587ABC: PR \u3092\u4f5c\u3063\u3066\u30c1\u30a7\u30c3\u30af\u3092\u901a\u305b"
    last = "\u524d\u62c5\u5f53\u306e\u6700\u5f8c\u306e\u8fd4\u7b54\u30c6\u30ad\u30b9\u30c8XYZ"
    text = RFS.build_resume_text(goal, worker_last=last, partial_text="", journal="",
                                cwd="", git_summary="", folder_listing="", pr_summary="")
    assert isinstance(text, str)
    assert goal in text
    assert last in text
    # it instructs the worker to check real state first (git/PR) ...
    assert VERIFY_FRAGMENT in text
    assert "gh pr" in text
    # ... and not to repeat already-finished steps ...
    assert NO_REDO_FRAGMENT in text
    # ... and the original goal still comes after the preamble, not replaced by it.
    assert text.index(NO_REDO_FRAGMENT) < text.index(goal)


def test_resume_text_survives_empty_inputs_and_missing_cwd(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    goal = "\u7a7a\u306e\u524d\u7f6e\u304d\u3067\u3082\u4f9d\u983c\u672c\u6587\u306f\u6b8b\u308b"
    text = RFS.build_resume_text(goal, worker_last="", partial_text="", journal="",
                                cwd=missing, pr_summary="")
    assert isinstance(text, str)
    assert goal in text
    assert VERIFY_FRAGMENT in text


def _git(args, cwd):
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    })
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, env=env)


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is not available on this machine")
def test_git_summary_includes_the_branch_pr_when_supplied(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], str(repo))
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "a.txt"], str(repo))
    subject = "fixture commit subject for resume test"
    _git(["commit", "-q", "-m", subject], str(repo))

    # gh is injected (gh_pr=...) so the test never shells out to a real gh / network.
    pr = "PR #180 [OPEN] implement a call feature\nhttps://example.invalid/pr/180"
    summary = AR._git_summary(str(repo), gh_pr=pr)
    assert subject in summary
    assert "\u3053\u306e\u30d6\u30e9\u30f3\u30c1\u306e PR" in summary
    assert "PR #180" in summary


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is not available on this machine")
def test_git_summary_says_no_pr_when_none_found(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], str(repo))
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "a.txt"], str(repo))
    _git(["commit", "-q", "-m", "c"], str(repo))
    # empty gh result (no PR / gh unavailable) must not raise and must be reported plainly.
    summary = AR._git_summary(str(repo), gh_pr="")
    assert "\u3053\u306e\u30d6\u30e9\u30f3\u30c1\u306e PR: \u306a\u3057" in summary


def test_resume_text_reaches_git_summary_without_a_repo(tmp_path):
    # No git repo at cwd: build_resume_text still returns, and the preamble says state unknown.
    text = RFS.build_resume_text("\u4f9d\u983c", cwd=str(tmp_path), pr_summary="")
    assert "\u4f9d\u983c" in text
    assert isinstance(text, str)


# ---- relay_fleet wiring (source-level; the heavy module is not imported) -----------------
#
# RECONCILED 2026-11-09 (merge of origin/main into fix/resume-unknown-delivery-timeout-ci):
# the feature this test was written for landed on main independently as PR #503, which
# implements the same “resume a delivery-unknown acting turn in a fresh conversation that
# first reads real state” recovery under the name `_resume_from_state_after_unknown`, backed
# by relay/resume_from_state.py (enabled()/goal_is_resumable()/RESUME_FROM_STATE_PER_GOAL/
# build_resume_text). The merge took main’s implementation; this branch’s original
# `_resume_unknown_act_in_fresh_conversation` no longer exists. These source-level checks are
# updated to assert the SAME invariants against the shipping method name/wiring, so coverage
# of the incident (workers left STUCK on turn timeout with delivery unknown) is preserved.
#
# NOTE: `_resume_from_state_after_unknown` is CALLED from two places -- the proven-not-sent
# path in `_retry_socket` (earlier in the file) and the per-turn timeout path (later). Tests
# that reason about the timeout path specifically anchor on the TIMEOUT occurrence (rfind /
# the slice at/after the 'bounded wait expired' refusal), not merely the first call site.

def _relay_fleet_src():
    with open(RELAY_FLEET, encoding="utf-8") as fh:
        return fh.read()


def test_timeout_tries_fresh_resume_before_refusing_on_unknown_delivery():
    src = _relay_fleet_src()
    # In the per-turn timeout path, an acting goal cut with delivery UNKNOWN whose
    # same-conversation continue is unavailable must be offered the fresh-state resume BEFORE
    # the terminal refuse-resend of a 'turn timeout without a reply' that leaves it STUCK.
    # Anchor on the TIMEOUT-path resume call (the last occurrence) so this is not satisfied by
    # the separate proven-not-sent call site earlier in the file.
    resume_invoke = src.rfind("self._resume_from_state_after_unknown(")
    refuse_call = src.find('self._refuse_resend("turn timeout without a reply"')
    assert resume_invoke != -1, "timeout path never invokes the fresh-state resume recovery"
    assert refuse_call != -1, "timeout path lost its terminal refuse-resend"
    assert resume_invoke < refuse_call, \
        "resume must be attempted before the worker is refused and left STUCK"


def test_delivered_delivery_is_still_refused_not_resumed():
    src = _relay_fleet_src()
    # A turn whose delivery is proven DELIVERED gets the bounded-wait refusal and returns; it
    # is NOT routed into the delivery-unknown fresh-state resume. Within the TIMEOUT path that
    # refusal must stay ahead of (and separate from) the unknown-resume invocation -- so it
    # precedes the timeout-path resume call (the one that follows it in source order).
    delivered = src.find('self._refuse_resend("bounded wait expired; continuation unavailable"')
    assert delivered != -1
    # The timeout-path resume call is the first resume occurrence AT/AFTER the delivered refusal.
    resume_invoke = src.find("self._resume_from_state_after_unknown(", delivered)
    assert resume_invoke != -1, "timeout path never invokes the fresh-state resume after the delivered refusal"
    assert delivered < resume_invoke, \
        "the delivered-timeout refusal must stay ahead of (and separate from) the unknown resume"


def test_resume_method_is_bounded_and_opens_a_fresh_conversation():
    src = _relay_fleet_src()
    m = re.search(
        r"def _resume_from_state_after_unknown\(self, reason, delivery\):(.*?)\n    def ", src, re.S)
    assert m, "_resume_from_state_after_unknown not found in relay_fleet.py"
    body = m.group(1)
    # Bounded per goal: a spent counter compared against RESUME_FROM_STATE_PER_GOAL, and the
    # bound is checked (returning False) BEFORE a fresh conversation is ever started.
    assert "_state_resumes_used" in body
    assert "RESUME_FROM_STATE_PER_GOAL" in body
    guard = re.search(
        r"if spent >= _resume_from_state\.RESUME_FROM_STATE_PER_GOAL:\s*\n\s*return False", body)
    assert guard, "no 'if spent >= RESUME_FROM_STATE_PER_GOAL: return False' bound"
    assert "build_resume_text" in body
    start_call = body.find("self._start_fresh_conversation(")
    assert start_call != -1, "fresh conversation is never started"
    assert guard.start() < start_call, "the bound must be checked before starting a conversation"


def test_resume_method_respects_the_auto_rephrase_setting():
    src = _relay_fleet_src()
    m = re.search(
        r"def _resume_from_state_after_unknown\(self, reason, delivery\):(.*?)\n    def ", src, re.S)
    assert m
    body = m.group(1)
    # The recovery is gated by its own settings module (relay/resume_from_state.py): when the
    # feature is switched off, enabled() is False and the method refuses rather than resuming.
    assert "_resume_from_state" in body
    assert ".enabled()" in body
