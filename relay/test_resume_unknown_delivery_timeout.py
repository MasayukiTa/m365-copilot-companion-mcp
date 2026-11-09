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

#: A stable fragment of the resume lead: the worker must verify state before acting.
VERIFY_FRAGMENT = "git status"
#: A stable fragment telling the worker not to redo finished work.
NO_REDO_FRAGMENT = "やり直さない"


def test_resume_text_carries_goal_state_check_and_no_redo_instruction():
    goal = "依頼本文ABC: PR を作ってチェックを通せ"
    last = "前担当の最後の返答テキストXYZ"
    text = AR.build_resume_text(goal, worker_last=last, partial_text="", journal="",
                               cwd="", git_summary="", folder_listing="")
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
    goal = "空の前置きでも依頼本文は残る"
    text = AR.build_resume_text(goal, worker_last="", partial_text="", journal="",
                               cwd=missing)
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
    assert "このブランチの PR" in summary
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
    assert "このブランチの PR: なし" in summary


def test_resume_text_reaches_git_summary_without_a_repo(tmp_path):
    # No git repo at cwd: build_resume_text still returns, and the preamble says state unknown.
    text = AR.build_resume_text("依頼", cwd=str(tmp_path))
    assert "依頼" in text
    assert isinstance(text, str)


# ---- relay_fleet wiring (source-level; the heavy module is not imported) -----------------

def _relay_fleet_src():
    with open(RELAY_FLEET, encoding="utf-8") as fh:
        return fh.read()


def test_timeout_tries_fresh_resume_before_refusing_on_unknown_delivery():
    src = _relay_fleet_src()
    # The resume is invoked via a callable lookup so the settle-test stand-in workers (which do
    # not subclass RelayWorker) fall back to the old refuse path; the recovery call still comes
    # BEFORE the terminal refuse-resend of a 'turn timeout without a reply'.
    resume_call = src.find("_resume_unknown_act_in_fresh_conversation")
    resume_invoke = src.find("and callable(_resume_fn) and _resume_fn()")
    refuse_call = src.find('self._refuse_resend("turn timeout without a reply"')
    assert resume_call != -1, "timeout path never references the fresh-resume recovery"
    assert resume_invoke != -1, "timeout path never invokes the fresh-resume recovery"
    assert refuse_call != -1, "timeout path lost its terminal refuse-resend"
    assert resume_invoke < refuse_call, \
        "resume must be attempted before the worker is refused and left STUCK"


def test_delivered_delivery_is_still_refused_not_resumed():
    src = _relay_fleet_src()
    delivered = src.find('self._refuse_resend("bounded wait expired; continuation unavailable"')
    resume_invoke = src.find("and callable(_resume_fn) and _resume_fn()")
    assert delivered != -1
    assert resume_invoke != -1
    assert delivered < resume_invoke, \
        "the delivered-timeout refusal must stay ahead of (and separate from) the unknown resume"


def test_resume_method_is_bounded_and_opens_a_fresh_conversation():
    src = _relay_fleet_src()
    m = re.search(
        r"def _resume_unknown_act_in_fresh_conversation\(self\):(.*?)\n    def ", src, re.S)
    assert m, "_resume_unknown_act_in_fresh_conversation not found in relay_fleet.py"
    body = m.group(1)
    assert "_resume_unknown_used" in body
    assert "FRESH_RETRY_MAX" in body
    guard = re.search(r"if used >= FRESH_RETRY_MAX:\s*\n\s*return False", body)
    assert guard, "no 'if used >= FRESH_RETRY_MAX: return False' bound"
    assert "build_resume_text" in body
    start_call = body.find("self._start_fresh_conversation(")
    assert start_call != -1, "fresh conversation is never started"
    assert guard.start() < start_call, "the bound must be checked before starting a conversation"


def test_resume_method_respects_the_auto_rephrase_setting():
    src = _relay_fleet_src()
    m = re.search(
        r"def _resume_unknown_act_in_fresh_conversation\(self\):(.*?)\n    def ", src, re.S)
    assert m
    body = m.group(1)
    assert "auto_rephrase_continue" in body
    assert ".enabled()" in body
