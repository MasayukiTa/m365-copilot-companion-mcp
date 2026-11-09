# -*- coding: utf-8 -*-
"""An acting repo goal cut with delivery UNKNOWN, with no same-conversation continue, is resumed
in a NEW conversation that first reads its real state -- instead of ending STUCK.

The 2026-11-09 bundle: a shared socket dropped (no close frame) and silenced two in-flight turns
(w41, w43) at once; both produced no output, so delivery is honestly unknown, the same-conversation
continue refused (the backend completed no exchange), and the timeout path refused to STUCK. With
resume_from_state ON (default) such a cut opens a fresh conversation whose first instruction is to
read git/PR/files and continue; OFF keeps the old refuse-to-STUCK. A goal that names a native act
(mail) is never resumed this way -- it cannot be reconciled from a file read.
"""
import time

import pytest

from relay import relay_fleet as RF
from relay import settle as S
from relay import resume_from_state as RFS
from relay import delivery_verify as DV


# ---- the setting -----------------------------------------------------------------------------

def test_setting_default_is_on_and_off_line_is_read(tmp_path):
    p = tmp_path / "settings.txt"
    p.write_text("", encoding="utf-8")
    assert RFS.mode(str(p)) == "on"                 # absent key -> on
    p.write_text("resume_from_state=off\n", encoding="utf-8")
    assert RFS.mode(str(p)) == "off"
    assert RFS.enabled(str(p)) is False
    p.write_text("resume_from_state=on\n", encoding="utf-8")
    assert RFS.enabled(str(p)) is True


# ---- eligibility: repo work yes, native acts no ----------------------------------------------

def test_repo_goal_is_resumable_and_mail_goal_is_not():
    assert RFS.goal_is_resumable("clone the repo, run pytest and open a PR") is True
    assert RFS.goal_is_resumable("8\u6708\u307e\u3067\u306epptx\u3068\u540c\u3058\u3082\u306e\u3092\u4f5c\u3063\u3066\u30b3\u30df\u30c3\u30c8\u3059\u308b") is True
    # Native acts cannot be reconciled from a git/file read -> not resumed this way.
    assert RFS.goal_is_resumable("\u30e1\u30fc\u30eb\u3067\u9001\u3063\u3066\u304f\u3060\u3055\u3044") is False
    assert RFS.goal_is_resumable("reply to the invite in Teams") is False


# ---- the opening text reads real state first -------------------------------------------------

def test_build_resume_text_prepends_read_state_then_preamble_then_goal():
    text = RFS.build_resume_text(
        "PR #184 \u3092\u76f4\u3059",
        worker_last="\u76f4\u3057\u3066\u3044\u307e\u3057\u305f", partial_text="", journal="",
        cwd="",
        git_summary="\u30d6\u30e9\u30f3\u30c1: fix/184\n\u76f4\u8fd1\u306e\u30b3\u30df\u30c3\u30c8:\nabc \u4fee\u6b63",
        folder_listing="a.py\nb.md",
        pr_summary="PR #184 [OPEN] fix\nhttps://example/pr/184")
    # Reads state first: the lead names git status / git log / gh pr view before the goal.
    assert "git status" in text and "git log" in text and "gh pr view" in text
    assert text.index("git status") < text.index("--- \u5143\u306e\u4f9d\u983c ---")
    # The machine-collected state (git + PR) is carried.
    assert "fix/184" in text
    assert "PR #184 [OPEN]" in text
    # The original goal is at the end.
    assert text.rstrip().endswith("PR #184 \u3092\u76f4\u3059")
    # And the explicit do-not-redo instruction is present.
    assert "\u4f5c\u308a\u76f4\u3055\u306a\u3044" in text


def test_build_resume_text_without_pr_still_tells_the_model_to_look():
    text = RFS.build_resume_text(
        "\u30d5\u30a1\u30a4\u30eb\u3092\u51fa\u3059", cwd="",
        git_summary="", folder_listing="", pr_summary="")
    assert "gh pr view" in text                     # the instruction is always there
    assert "PR \u72b6\u614b" in text


# ---- fleet behaviour: resume instead of STUCK ------------------------------------------------

class _Answers:
    def count(self):
        return 0


class _Driver:
    def __init__(self):
        self._turn_answered = False
        self._thread = None
        self.conv = None

    def _answers(self):
        return _Answers()

    def _is_generating(self):
        return False

    def partial_text(self):
        return ""

    def close(self):
        pass


class _Worker:
    poll = RF.RelayWorker.poll
    _saw_output_for_turn = RF.RelayWorker._saw_output_for_turn
    _resume_from_state_after_unknown = RF.RelayWorker._resume_from_state_after_unknown
    _collect_progress_fields = RF.RelayWorker._collect_progress_fields
    _start_fresh_conversation = RF.RelayWorker._start_fresh_conversation
    status = "waiting"
    per_turn_timeout_s = 240
    dwell_s = 4.0
    _count_before = 0
    transient = 0
    max_transient = 10
    last_response = ""
    turn = 2
    cwd = ""
    _composed_prefix = ""

    def __init__(self, goal):
        self.goal = goal
        self.name = "w41"
        self.drv = _Driver()
        self._t_send = time.time() - 300
        self._last_text = None
        self._stable_since = None
        self._settle_state = S.SettleState()
        self.outcome = None
        self.reason = ""
        self.refused = None
        self.retryable_override = None
        self.notes = []
        self.job = ""
        self._cooldown_until = 0.0

    # -- stubs the poll timeout branch reaches
    def _capture_url(self):
        pass

    def _goal_may_act(self):
        return True

    def _effect_checker(self):
        return None

    def _timeout_resend_decision(self):
        return "refuse"

    def _note_timeout(self, origin, elapsed, kind, budget_s=None):
        self.notes.append(kind)

    def _salvage_via_checks(self):
        return False

    def _retry_transient(self):
        return False

    def _refuse_resend(self, reason, delivery):
        self.refused = (reason, delivery)
        self.status, self.outcome = "stuck", "STUCK"
        self.reason = "not re-sent"

    def _compaction_note(self):
        return ""


class _Route:
    def record(self, *a, **k):
        pass

    def open(self):
        return True


@pytest.fixture
def fleet_env(monkeypatch):
    """Only the fleet-behaviour tests need delivery-verify on and the I/O-free resume builder."""
    monkeypatch.setattr(DV, "verify_on", lambda path=None: True)
    monkeypatch.setattr(RFS, "enabled", lambda path=None: True)
    monkeypatch.setattr(RFS, "build_resume_text", lambda goal_body, **kw: "[resume] " + goal_body)
    monkeypatch.setattr(RF, "_socket_route", lambda: _Route())


def test_acting_repo_timeout_resumes_from_state_not_stuck(fleet_env):
    w = _Worker("clone the repo, run pytest and commit the fix")
    out = w.poll()
    assert w.refused is None, "should not refuse-to-STUCK when a state resume is available"
    assert w.outcome != "STUCK"
    assert "resume-from-state" in w.notes
    assert w.status == "ready"                      # queued for the fresh conversation
    assert "[resume]" in w.job
    assert getattr(w, "_state_resumes_used", 0) == 1
    assert out is False                             # worker continues, not terminal


def test_native_act_goal_still_stucks(fleet_env, monkeypatch):
    monkeypatch.setattr(RFS, "goal_is_resumable", lambda g: False)
    w = _Worker("\u30e1\u30fc\u30eb\u3067\u6708\u6b21\u30ec\u30dd\u30fc\u30c8\u3092\u9001\u308b")
    out = w.poll()
    assert w.refused is not None
    assert w.outcome == "STUCK"
    assert "resend-refused" in w.notes
    assert out is True


def test_resume_is_capped_per_goal(fleet_env):
    w = _Worker("clone the repo and open a PR")
    w._state_resumes_used = RFS.RESUME_FROM_STATE_PER_GOAL   # budget already spent
    out = w.poll()
    assert w.refused is not None                    # no second resume; refuse-to-STUCK
    assert w.outcome == "STUCK"
    assert out is True


def test_setting_off_keeps_old_refuse(fleet_env, monkeypatch):
    monkeypatch.setattr(RFS, "enabled", lambda path=None: False)
    w = _Worker("clone the repo and run the tests")
    out = w.poll()
    assert w.refused is not None
    assert w.outcome == "STUCK"
    assert out is True
