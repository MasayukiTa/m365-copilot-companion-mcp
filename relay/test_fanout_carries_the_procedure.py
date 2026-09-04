# -*- coding: utf-8 -*-
"""The worker asked to SPLIT a goal was the one denied the procedure for splitting it.

Turn 1 of a fan-out asks for a division rather than the work, and the branch that builds it
rebuilt the job from scratch -- PROTOCOL + goal + SPLIT_JOB -- discarding the matched
procedure and the theme memory that every other worker gets.

That is backwards for at least one approved Skill. mail-lookup's 大原則2 is a table of how to
slice a mail request by date range: a month becomes 上旬/中旬/下旬, a quarter becomes months,
and the reason given is that asking for a whole period at once is always truncated. It also
forbids re-fetching a range already collected -- and what was already collected is exactly
what the theme notes hold.

So the splitter was inventing its own slicing, against the wall the procedure exists to
describe, while the workers that received the procedure had no splitting to do.
"""
import io
import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import relay_fleet as F  # noqa: E402

MAIL_GOAL = "1月から4月のメールを一覧して、件名と差出人を出して"

SPLIT_SKILL = """---
name: mail-split-drill
description: "メールを期間で区切って一覧し、件名と差出人を報告するときに使う。Use when listing mail
  over a date range and reporting subjects and senders."
---

# メール一覧の手順

依頼を受けたら、まず期間を区切る。1ヶ月なら上旬・中旬・下旬の3つ。

MARKER-SLICING-RULE
"""


@pytest.fixture(autouse=True)
def approved_skill(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    d = root / "skills" / "mail-split-drill"
    os.makedirs(str(d))
    with io.open(str(d / "SKILL.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(SPLIT_SKILL)
    db = tmp_path / "s.sqlite3"
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(db))

    from relay.skills import SkillStore
    store = SkillStore(str(root))
    found = store.discover()
    assert found, "the fixture wrote no readable bundle"
    for skill in found:
        store.request_approval(skill.name)
        with sqlite3.connect(str(db)) as con:
            row = con.execute("SELECT gate_token FROM approval_challenges "
                              "ORDER BY created_at DESC").fetchone()
        store.confirm_approval(skill.name, row[0])
    return root


def test_the_splitter_is_given_the_procedure_for_splitting():
    """THE DEFECT. This branch rebuilt the job and dropped the one thing it needed."""
    w = F.RelayWorker(MAIL_GOAL, "w0", fanout=True)
    assert w.fanout is True, "fixture is not exercising the fan-out branch"
    assert "MARKER-SLICING-RULE" in w.job, "the splitter got no procedure"


def test_the_split_instruction_is_still_there():
    from relay import fanout as fanout_mod
    w = F.RelayWorker(MAIL_GOAL, "w0", fanout=True)
    assert fanout_mod.SPLIT_JOB in w.job


def test_the_goal_still_travels_in_full():
    """An agent asked to divide a goal it cannot see divides something else."""
    w = F.RelayWorker(MAIL_GOAL, "w0", fanout=True)
    assert MAIL_GOAL in w.job


def test_the_procedure_comes_before_the_instruction_to_split():
    """Read the HOW, then what is being asked of this turn."""
    from relay import fanout as fanout_mod
    w = F.RelayWorker(MAIL_GOAL, "w0", fanout=True)
    assert w.job.index("MARKER-SLICING-RULE") < w.job.index(fanout_mod.SPLIT_JOB)


def test_an_ordinary_worker_is_unchanged():
    """The non-fan-out path already carried the procedure and must keep doing so."""
    w = F.RelayWorker(MAIL_GOAL, "w0")
    assert w.fanout is False
    assert "MARKER-SLICING-RULE" in w.job


def test_the_match_is_not_run_twice_per_worker():
    """The composition is held in a local. Matching costs a filesystem walk, a digest per
    bundle and a SQLite read; doing it once per worker rather than twice is the difference
    between 63ms and 126ms on every construction."""
    calls = []
    real = F._with_matched_skill

    def counted(goal_text):
        calls.append(goal_text)
        return real(goal_text)

    F._with_matched_skill = counted
    try:
        F.RelayWorker(MAIL_GOAL, "w0", fanout=True)
    finally:
        F._with_matched_skill = real
    assert len(calls) == 1, "matched %d times for one worker" % len(calls)


# -- the two branches that hand the agent a chat with no history ----------------------------

def test_a_recycled_conversation_still_carries_the_procedure():
    """A token-limit recycle opens a BRAND NEW chat -- the agent has no memory of anything,
    including the procedure it was given at turn 1. It was re-anchored with PROTOCOL and the
    bare goal.

    Calls the builder the branch calls. The first version of this test reassembled the string
    itself, which would have kept passing if the branch stopped using it -- an assertion about
    my arithmetic rather than about the code.
    """
    from relay.copilot_autopilot_relay import RECYCLE_PREFIX
    w = F.RelayWorker(MAIL_GOAL, "w0")
    w._recycles = 1
    job = w._recycle_job()
    assert "MARKER-SLICING-RULE" in job, "the recycled chat got no procedure"
    assert job.index("MARKER-SLICING-RULE") < job.index(RECYCLE_PREFIX), (
        "the reset notice ends with a heading that should introduce the goal, so the "
        "procedure belongs above it")
    assert job.endswith(MAIL_GOAL)


def test_a_replayed_conversation_still_carries_the_procedure():
    """Its comment said "the same initial payload as the original", which stopped being true
    when the original grew memory and a procedure."""
    w = F.RelayWorker(MAIL_GOAL, "w0")
    w.fresh_replay_count = 1
    job = w._replay_job()
    assert "MARKER-SLICING-RULE" in job
    assert job.endswith(MAIL_GOAL)


def test_the_recycled_goal_is_not_duplicated():
    """The prefix is the composition MINUS the goal; getting that wrong sends the goal twice."""
    w = F.RelayWorker(MAIL_GOAL, "w0")
    w._recycles = 1
    assert w._recycle_job().count(MAIL_GOAL) == 1


def test_the_prefix_is_the_context_without_the_goal():
    """Taken by suffix, which is safe only because the composition ends with the goal --
    the property test_the_goal_survives_intact_and_last exists to hold."""
    w = F.RelayWorker(MAIL_GOAL, "w0")
    assert w._composed_goal.endswith(MAIL_GOAL)
    assert w._composed_prefix + MAIL_GOAL == w._composed_goal
    assert MAIL_GOAL not in w._composed_prefix


def test_a_goal_with_no_match_has_an_empty_prefix_not_a_broken_one():
    w = F.RelayWorker("x", "w0")
    assert w._composed_prefix + "x" == w._composed_goal
