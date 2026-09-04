# -*- coding: utf-8 -*-
"""The approved procedure never reached the workers that were ordered to fetch it.

The server tells every worker, as RULE 2, to call skill_match before any domain work. Measured
over the tool ledger:

    skill_match   178 calls, 145 dead on a guessed argument name (query= vs text=)
                  of those, 96 abandoned -- it is a preliminary step, so when it fails the
                  agent simply proceeds to the real work
                  33 successes in the entire ledger

Meanwhile the procedure that would have answered, repo-bug-fix, scores 1.0 on the very queries
that failed. The store was not empty and the matcher was not weak; the call never arrived.

SkillStore.match is deterministic, metadata-only and stdlib, and the frame holds the goal text
before the first prompt is composed. So the lookup needs no agent, no turn, and no correctly
guessed keyword.
"""
import os
import shutil
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import relay_fleet as F  # noqa: E402


@pytest.fixture(autouse=True)
def trusted_skills(tmp_path, monkeypatch):
    """A project root whose procedures are approved, built here rather than assumed.

    CAUGHT BY CI, WHICH IS THE POINT. The first version of these tests passed locally and
    failed on the runner with "no procedure was injected for a goal that matches at 1.0" --
    because a Skill is only injectable once a human has approved that exact bundle digest,
    and approval lives in a per-machine SQLite file that a fresh checkout does not have. The
    tests were reading this developer's approval database and calling it the behaviour of
    the code.

    So the fixture copies the real skills/ directory, points the store at a temp state DB,
    and approves through the store's own request/confirm pair -- the same path a human takes.
    The procedures under test are the real ones; only the trust is local.
    """
    root = tmp_path / "proj"
    shutil.copytree(os.path.join(REPO, "skills"), str(root / "skills"))
    db = tmp_path / "skills.sqlite3"
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(db))

    from relay.skills import SkillStore
    store = SkillStore(str(root))
    for skill in store.discover():
        store.request_approval(skill.name)
        with sqlite3.connect(str(db)) as con:
            row = con.execute(
                "SELECT gate_token FROM approval_challenges ORDER BY created_at DESC"
            ).fetchone()
        assert row and row[0], "no approval challenge was recorded for %s" % skill.name
        store.confirm_approval(skill.name, row[0])
    assert store.discover(), "the fixture approved nothing; skills/ did not copy"
    return root

# A real SWE-bench goal, in the shape the runner actually produces.
CODING_GOAL = ("You are fixing a real bug in the open-source project ansible "
               "(language: python). The repository is checked out locally at C:/w/p05")
UNRELATED = "今日の東京の天気を一行で教えて"


def test_a_matching_goal_gets_the_procedure_without_the_agent_asking():
    got = F._with_matched_skill(CODING_GOAL)
    assert got != CODING_GOAL, "no procedure was injected for a goal that matches at 1.0"
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


# -- the trap this nearly walked into ----------------------------------------------------

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
    # The wrapper must key on what it is TOLD to, not on what it was handed.
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


# -- the hazard the fanout suite caught the moment this was wired -------------------------

def test_a_procedure_that_introduces_the_split_marker_is_refused():
    """FOUND BY RUNNING THE SUITE, not by thinking about it.

    mail-lookup's body explains the split convention, so it contains the literal
    SUBTASKS_READY. fanout_ready() scans the REPLY rather than the prompt, so nothing fires
    directly -- but a worker that reads "write SUBTASKS_READY on the last line" can write it,
    and an ordinary task is then read as a proposed split.
    """
    goal = "先月分のメールを一覧して、件名と差出人だけ出して"
    got = F._with_matched_skill(goal)
    assert "SUBTASKS_READY" not in got, (
        "a procedure introduced a control word this worker is not given by default")


def test_the_markers_PROTOCOL_already_carries_do_not_block_a_procedure():
    """THE FIRST VERSION OF THE GUARD WAS WRONG and would have blocked the skill that matters.

    It banned every marker in control_markers.KINDS. But PROTOCOL itself names DONE /
    CONTINUE / STUCK / RESEARCH / ANALYZE, so repo-bug-fix saying 'write STUCK only when you
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


def test_an_unapproved_procedure_is_never_injected(tmp_path, monkeypatch):
    """WHAT CI WAS ACTUALLY SAYING. On a machine where nothing has been approved -- a fresh
    checkout, the runner, a new install -- the frame injects nothing at all. That is the
    trust model working, and it is why the fixture above has to build its own."""
    empty = tmp_path / "empty"
    (empty / "skills").mkdir(parents=True)
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(empty))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(tmp_path / "none.sqlite3"))
    assert F._with_matched_skill(CODING_GOAL) == CODING_GOAL


def test_an_untrusted_bundle_present_on_disk_is_still_refused(tmp_path, monkeypatch):
    """Trust is per DIGEST, so a procedure sitting in skills/ is not thereby usable."""
    root = tmp_path / "untrusted"
    shutil.copytree(os.path.join(REPO, "skills"), str(root / "skills"))
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(tmp_path / "fresh.sqlite3"))
    assert F._with_matched_skill(CODING_GOAL) == CODING_GOAL
