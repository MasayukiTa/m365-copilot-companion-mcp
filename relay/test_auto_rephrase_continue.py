# -*- coding: utf-8 -*-
"""Pure unit tests for the acting-goal rephrase-and-continue replay.

Covers relay/auto_rephrase_continue.py (build_resend_text) and the one-shot guard wiring in
relay/relay_fleet.py (_recycle_acting_goal_with_progress / _acting_rephrase_done). No network,
no browser. The relay_fleet check reads the source as text so the heavy module is not imported.
"""
from __future__ import annotations

import os
import re
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY_FLEET = os.path.join(REPO, "relay", "relay_fleet.py")

import sys  # noqa: E402

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import auto_rephrase_continue as AR  # noqa: E402

#: A stable fragment of the Japanese wording in progress_preamble ("すでにあるものを作り直さない").
CONTINUE_FRAGMENT = "すでにあるものを作り直さない"


def test_resend_text_carries_goal_last_reply_and_continue_instruction():
    goal = "依頼本文ABC: ファイルを作れ"
    last = "前担当の最後の返答テキストXYZ"
    text = AR.build_resend_text(goal, worker_last=last, partial_text="", journal="",
                                cwd="", git_summary="", folder_listing="")
    assert isinstance(text, str)
    assert goal in text
    assert last in text
    assert CONTINUE_FRAGMENT in text
    # the original goal comes after the preamble, not replaced by it
    assert text.index(CONTINUE_FRAGMENT) < text.index(goal)


def test_resend_text_survives_empty_inputs_and_missing_cwd(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    goal = "空の前置きでも依頼本文は残る"
    text = AR.build_resend_text(goal, worker_last="", partial_text="", journal="",
                                cwd=missing)
    assert isinstance(text, str)
    assert goal in text
    assert CONTINUE_FRAGMENT in text


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
def test_resend_text_mentions_repo_state_from_a_real_git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], str(repo))
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "a.txt"], str(repo))
    subject = "fixture commit subject for rephrase test"
    _git(["commit", "-q", "-m", subject], str(repo))

    text = AR.build_resend_text("依頼", worker_last="", partial_text="", journal="",
                                cwd=str(repo))
    # _git_summary collects the last commits via `git log --oneline`, so the subject shows.
    assert subject in text
    assert "直近のコミット" in text
    assert "依頼" in text


def test_relay_fleet_sets_one_shot_guard_before_starting_fresh_conversation():
    with open(RELAY_FLEET, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"def _recycle_acting_goal_with_progress\(self\):(.*?)\n    def ", src, re.S)
    assert m, "_recycle_acting_goal_with_progress not found in relay_fleet.py"
    body = m.group(1)
    guard_set = body.find("self._acting_rephrase_done = True")
    start_call = body.find("self._start_fresh_conversation(")
    assert guard_set != -1, "guard is never set to True"
    assert start_call != -1, "fresh conversation is never started"
    assert guard_set < start_call, "guard must be set before the fresh conversation starts"


def test_relay_fleet_returns_false_when_guard_already_spent():
    with open(RELAY_FLEET, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"def _recycle_acting_goal_with_progress\(self\):(.*?)\n    def ", src, re.S)
    assert m
    body = m.group(1)
    # the first executable statement after the docstring must be the guard check returning False
    guard = re.search(r"if self\._acting_rephrase_done:\s*\n\s*return False", body)
    assert guard, "no early 'if self._acting_rephrase_done: return False' guard"
    assert guard.start() < body.find("self._acting_rephrase_done = True")
