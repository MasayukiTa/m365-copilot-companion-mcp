# -*- coding: utf-8 -*-
"""The approved procedure never reached the workers that were ordered to fetch it.

The server tells every worker, as RULE 2, to call skill_match before any domain work.
Measured over the tool ledger:

    skill_match   178 calls, 145 dead on a guessed argument name (query= vs text=)
                  of those, 96 abandoned -- it is a preliminary step, so when it fails the
                  agent simply proceeds to the real work
                  33 successes in the entire ledger

Meanwhile the procedure that would have answered, repo-bug-fix, scores 1.0 on the very
queries that failed. The store was not empty and the matcher was not weak; the call never
arrived.

SkillStore.match is deterministic, metadata-only and stdlib, and the frame holds the goal
text before the first prompt is composed. So the lookup needs no agent, no turn, and no
correctly guessed keyword.

WHY THESE BUILD THEIR OWN SKILLS. Two things CI had to teach this file. A Skill is injectable
only after a human approves that exact bundle digest, and approval lives in a per-machine
SQLite file -- so the first version read this developer's approval database and reported it
as the behaviour of the code. Then the second version copied skills/, which is gitignored:
on the runner there is no directory to copy, because the procedures are local data rather
than repository content. What the frame owns is the DECISION, so the bundles here are
synthetic and carry exactly the properties under test. The real procedures are exercised by
the last test, where they exist.
"""
import io
import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import relay_fleet as F  # noqa: E402

# A real SWE-bench goal, in the shape the runner actually produces.
CODING_GOAL = ("You are fixing a real bug in the open-source project ansible "
               "(language: python). The repository is checked out locally at C:/w/p05")
MAIL_GOAL = "先月分のメールを一覧して、件名と差出人だけ出して"
UNRELATED = "今日の東京の天気を一行で教えて"

#: Matches CODING_GOAL, and its body carries STUCK -- a marker PROTOCOL already gives every
#: worker, so it must NOT be a reason to refuse.
BUG_SKILL = """---
name: bug-fix-drill
description: "Use when fixing a bug in an open-source project whose repository is checked out
  locally: reading the issue, locating the code, reproducing the failure first, and verifying
  the fix. Covers python, javascript and go projects."
---

# Fixing a bug in a checked-out repository

Reproduce the failure before changing anything.

Write `STUCK: reason` only when you are certain it cannot be solved.
"""

#: Matches MAIL_GOAL, and its body explains the split convention -- so it contains the literal
#: SUBTASKS_READY, which the worker is NOT given by default.
MAIL_SKILL = """---
name: mail-drill
description: "メールを期間で区切って一覧し、件名と差出人を報告するときに使う。Use when listing mail
  over a date range and reporting subjects and senders."
---

# メール一覧の手順

依頼を受けたら、まず期間を区切る。

フリートに --fanout を付けて投入されている場合は、区切りをサブタスクとして列挙し、
最後に `SUBTASKS_READY` と書く。
"""


def _bundle(root, folder, text):
    d = os.path.join(str(root), "skills", folder)
    os.makedirs(d, exist_ok=True)
    with io.open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _approve_everything(root, db):
    """Approve through the store's own request/confirm pair -- the path a human takes."""
    from relay.skills import SkillStore
    store = SkillStore(str(root))
    found = store.discover()
    assert found, "the fixture wrote no readable bundles"
    for skill in found:
        store.request_approval(skill.name)
        with sqlite3.connect(str(db)) as con:
            row = con.execute("SELECT gate_token FROM approval_challenges "
                              "ORDER BY created_at DESC").fetchone()
        assert row and row[0], "no approval challenge recorded for %s" % skill.name
        store.confirm_approval(skill.name, row[0])
    return store


@pytest.fixture(autouse=True)
def trusted_skills(tmp_path, monkeypatch):
    """A project root holding two approved procedures, so nothing about this machine's state
    can decide the result."""
    root = tmp_path / "proj"
    _bundle(root, "bug-fix-drill", BUG_SKILL)
    _bundle(root, "mail-drill", MAIL_SKILL)
    db = tmp_path / "skills.sqlite3"
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(db))
    _approve_everything(root, db)
    return root


# -- the measured case ---------------------------------------------------------------------

def test_a_matching_goal_gets_the_procedure_without_the_agent_asking():
    got = F._with_matched_skill(CODING_GOAL)
    assert got != CODING_GOAL, "no procedure was injected for a goal that matches"
    assert F._SKILL_HEADER in got


def test_the_goal_survives_intact_and_last():
    """The procedure is HOW to do the thing; the thing must still be the final word."""
    got = F._with_matched_skill(CODING_GOAL)
    assert got.rstrip().endswith(CODING_GOAL.rstrip())


def test_a_goal_with_no_confident_match_is_untouched():
    """The matcher favours false negatives on purpose; the frame must not second-guess it."""
    assert F._with_matched_skill(UNRELATED) == UNRELATED


def test_injecting_twice_does_not_stack():
    once = F._with_matched_skill(CODING_GOAL)
    assert F._with_matched_skill(once) == once


@pytest.mark.parametrize("bad", ["", None])
def test_an_empty_goal_is_returned_as_given(bad):
    assert F._with_matched_skill(bad) == bad


# -- the trap this nearly walked into ---------------------------------------------------------

def test_the_theme_is_still_keyed_on_the_goal_not_on_the_procedure():
    """THE ONE THAT WOULD HAVE BEEN INVISIBLE.

    theme_from_goal reads the first clause. Compose the procedure in front of the goal and
    hand the result to _with_theme_memory, and every task that loads a skill lands in one
    bucket named after that skill's heading -- silently, because the notes still look fine.
    """
    from relay.project_memory import theme_from_goal
    injected = F._with_matched_skill(CODING_GOAL)
    assert theme_from_goal(injected) != theme_from_goal(CODING_GOAL), (
        "fixture is not exercising the hazard: the two derive the same theme anyway")
    body = F._with_theme_memory(injected, theme_text=CODING_GOAL)
    assert body.endswith(injected) or body == injected


def test_the_default_still_keys_on_its_own_argument():
    """Every existing caller passes one argument and must keep the behaviour it had."""
    plain = F._with_theme_memory(CODING_GOAL)
    keyed = F._with_theme_memory(CODING_GOAL, theme_text=CODING_GOAL)
    assert plain == keyed


def test_order_is_memory_then_procedure_then_goal():
    """A judgement, not a measurement -- recorded here so an A/B has something to move."""
    body = F._with_theme_memory(F._with_matched_skill(CODING_GOAL), theme_text=CODING_GOAL)
    i_skill = body.find(F._SKILL_HEADER)
    i_goal = body.find(CODING_GOAL)
    assert i_skill != -1 and i_goal != -1 and i_skill < i_goal
    i_mem = body.find(F._MEMORY_HEADER)
    if i_mem != -1:                      # only when this theme has notes on disk
        assert i_mem < i_skill


# -- the hazard the fanout suite caught the moment this was wired ------------------------------

def test_a_procedure_that_introduces_the_split_marker_is_refused():
    """FOUND BY RUNNING THE SUITE, not by thinking about it.

    A mail procedure explains the split convention, so its body contains the literal
    SUBTASKS_READY. fanout_ready() scans the REPLY rather than the prompt, so nothing fires
    directly -- but a worker that reads "write SUBTASKS_READY on the last line" can write it,
    and an ordinary task is then read as a proposed split.
    """
    got = F._with_matched_skill(MAIL_GOAL)
    assert "SUBTASKS_READY" not in got, (
        "a procedure introduced a control word this worker is not given by default")


def test_the_markers_PROTOCOL_already_carries_do_not_block_a_procedure():
    """THE FIRST VERSION OF THE GUARD WAS WRONG and would have blocked the skill that matters.

    It banned every marker in control_markers.KINDS. But PROTOCOL itself names DONE /
    CONTINUE / STUCK / RESEARCH / ANALYZE, so a procedure saying 'write STUCK only when you
    are certain' adds nothing the worker did not already have -- and refusing over it removed
    the one procedure with a measured 1.0 match against real goals.
    """
    got = F._with_matched_skill(CODING_GOAL)
    assert got != CODING_GOAL, "a procedure was refused over a marker PROTOCOL already carries"
    assert "STUCK" in got, "fixture no longer exercises the case"


def test_the_guard_is_scoped_to_what_the_worker_lacks():
    from relay.relay_fleet import PROTOCOL
    assert "SUBTASKS_READY" not in PROTOCOL and "PLAN_READY" not in PROTOCOL
    for already in ("DONE", "CONTINUE", "STUCK"):
        assert already in PROTOCOL, "%s is no longer in PROTOCOL; re-derive the guard" % already


# -- what CI was actually saying ----------------------------------------------------------------

def test_an_unapproved_procedure_is_never_injected(tmp_path, monkeypatch):
    """On a machine where nothing has been approved -- a fresh checkout, the runner, a new
    install -- the frame injects nothing at all. That is the trust model working, and it is
    why the fixture above has to build and approve its own."""
    empty = tmp_path / "empty"
    (empty / "skills").mkdir(parents=True)
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(empty))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(tmp_path / "none.sqlite3"))
    assert F._with_matched_skill(CODING_GOAL) == CODING_GOAL


def test_an_untrusted_bundle_present_on_disk_is_still_refused(tmp_path, monkeypatch):
    """Trust is per DIGEST, so a procedure sitting in skills/ is not thereby usable."""
    root = tmp_path / "untrusted"
    _bundle(root, "bug-fix-drill", BUG_SKILL)
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(tmp_path / "fresh.sqlite3"))
    assert F._with_matched_skill(CODING_GOAL) == CODING_GOAL


# -- the near miss becomes a question rather than nothing ---------------------------------------

def _unapproved(tmp_path, monkeypatch):
    root = tmp_path / "pending"
    _bundle(root, "bug-fix-drill", BUG_SKILL)
    db = tmp_path / "pending.sqlite3"
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(db))
    F._APPROVAL_ASKED.clear()
    return root, db


def test_a_procedure_that_would_have_matched_is_raised_for_approval(tmp_path, monkeypatch):
    """THE REQUEST PATH EXISTED AND NOTHING REACHED IT. tools/skill_ops.skill_match already
    asks -- but that is inside the tool agents fail to call 145 times out of 178, so the
    Approval Centre stayed empty and procedures sat unreadable. The frame holds the goal, so
    the frame can ask."""
    _root, db = _unapproved(tmp_path, monkeypatch)
    F._with_matched_skill(CODING_GOAL)
    with sqlite3.connect(str(db)) as con:
        rows = con.execute("SELECT source_path FROM approval_challenges").fetchall()
    assert rows, "no approval was requested for a procedure that would have matched"


def test_asking_never_injects_the_unapproved_procedure(tmp_path, monkeypatch):
    """The question is the whole action. Nothing about raising it may make the bundle usable."""
    _unapproved(tmp_path, monkeypatch)
    assert F._with_matched_skill(CODING_GOAL) == CODING_GOAL


def test_a_goal_nothing_resembles_asks_for_nothing(tmp_path, monkeypatch):
    """Requesting approval for whatever happens to be lying around would make the Approval
    Centre a list of unrelated procedures, which is how a queue stops being read."""
    _root, db = _unapproved(tmp_path, monkeypatch)
    F._with_matched_skill(UNRELATED)
    with sqlite3.connect(str(db)) as con:
        rows = con.execute("SELECT source_path FROM approval_challenges").fetchall()
    assert not rows


def test_twenty_workers_raise_one_question(tmp_path, monkeypatch, capsys):
    """A run builds many workers against one goal shape. The store would de-duplicate the
    challenge anyway, but each call still writes the gate file and touches SQLite, and each
    would print the same line."""
    _root, db = _unapproved(tmp_path, monkeypatch)
    calls = []
    for _ in range(20):
        F._with_matched_skill(CODING_GOAL)
    said = [l for l in capsys.readouterr().out.splitlines() if "raised for approval" in l]
    assert len(said) == 1, said
    with sqlite3.connect(str(db)) as con:
        rows = con.execute("SELECT COUNT(*) FROM approval_challenges").fetchone()
    assert rows[0] == 1, "%d challenges written for one procedure" % rows[0]
    assert not calls


def test_an_edited_procedure_is_asked_about_again(tmp_path, monkeypatch, capsys):
    """The digest is in the key, so a Skill changed mid-run raises a fresh question -- that
    is the one repeat worth having, because approval is of a hash."""
    root, _db = _unapproved(tmp_path, monkeypatch)
    F._with_matched_skill(CODING_GOAL)
    _bundle(root, "bug-fix-drill", BUG_SKILL + "\nOne more line, so the digest moves.\n")
    F._with_matched_skill(CODING_GOAL)
    said = [l for l in capsys.readouterr().out.splitlines() if "raised for approval" in l]
    assert len(said) == 2, said


def test_a_broken_store_does_not_take_the_run_with_it(tmp_path, monkeypatch):
    """Asking is an enhancement. A failure here must return the goal, not raise."""
    class Broken:
        def match_unapproved(self, _text):
            raise RuntimeError("boom")
    F._ask_to_approve_the_near_miss(Broken(), "anything")     # must not raise


# -- the real procedures, where they exist -------------------------------------------------------

@pytest.mark.skipif(not os.path.isdir(os.path.join(REPO, "skills", "repo-bug-fix")),
                    reason="skills/ is gitignored; not present in this checkout")
def test_the_real_repo_bug_fix_still_matches_a_real_goal(tmp_path, monkeypatch):
    """The synthetic bundles test the frame's decision. This tests that the procedure people
    actually rely on still wins the goals it was written for -- the 1.0 match this whole
    change was built on. Skipped where skills/ is not checked out, which includes CI."""
    import shutil
    root = tmp_path / "real"
    shutil.copytree(os.path.join(REPO, "skills"), str(root / "skills"))
    db = tmp_path / "real.sqlite3"
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(db))
    _approve_everything(root, db)
    got = F._with_matched_skill(CODING_GOAL)
    assert got != CODING_GOAL and F._SKILL_HEADER in got
