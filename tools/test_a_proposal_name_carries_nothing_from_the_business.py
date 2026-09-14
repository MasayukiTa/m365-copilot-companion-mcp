# -*- coding: utf-8 -*-
"""A generated Skill proposal must not name itself after the work it came from.

WHY THIS TEST EXISTS RATHER THAN A BLOCKLIST. `skill_draft` reads goal text, and goal text is
business content: colleagues' names, an employee id, the company name, internal paths. The first
version of `_slug` built the bundle name out of the goal's ascii words because that reads well,
and the first real run -- before anything was written to disk -- produced names carrying an
employee id, the company name, and three colleagues' surnames.

A name becomes a DIRECTORY name. Directory names show up in listings, in shell history, in
`git status`, and are one `git add` from a public repository. This repository has put identifying
content into a public history three times; the last one required rewriting it.

The blocklist route was considered and rejected as unprovable: the configured secret words catch
the company name and the employee-id shape, and nothing catches a surname. So the rule tested
here is the only one that can be shown to hold -- **no character of the goal reaches the name**.
"""
from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import skill_draft  # noqa: E402

#: Invented for the test -- no real person, path or identifier appears here.
#:
#: AND DELIBERATELY NOT SHAPED LIKE A REAL EMPLOYEE ID. The first draft of this fixture used an
#: invented id of the same letter-digit shape as the real one, and the naming gate flagged the
#: file: a gate cannot tell an invented identifier from a real one, and neither can a reader of
#: a public repository. "It is fake" is an argument that only works if you already know.
GOALS = [
    r"C:\Users\someone\example-internal\reports で四半期の検査結果をまとめてください。",
    "田中太郎さんと佐藤花子さんの内線番号を一覧にしてください。",
    "Summarise the Contoso Holdings supplier audit for Yamada Ichiro before friday.",
    "",
    "777から289を引くといくつか、数字だけ答えて",
]


def test_no_word_of_the_goal_survives_into_the_name():
    for goal in GOALS:
        name = skill_draft._slug(goal)
        assert re.match(r"^work-[0-9a-f]{10}$", name), (
            "a proposal name must be opaque, got %r for a goal that contains business text"
            % name)
        for word in re.findall(r"[A-Za-z0-9]{3,}", goal):
            assert word.lower() not in name.lower(), (
                "%r from the goal reached the bundle name %r" % (word, name))


def test_the_same_work_keeps_the_same_name():
    # Otherwise every run proposes a "new" Skill for work already proposed, and the proposals
    # directory fills with duplicates nobody can tell apart.
    assert skill_draft._slug(GOALS[0]) == skill_draft._slug(GOALS[0])


def test_different_work_gets_different_names():
    names = {skill_draft._slug(g) for g in GOALS}
    assert len(names) == len(GOALS), "two different goals collided on one bundle name"


def test_proposals_are_never_written_into_the_skills_directory():
    """Two separate harms, one rule.

    A SKILL.md is human-edited text, so writing one destroys somebody's decision. And a Skill is
    trusted by the digest of its whole bundle (`relay/skills.py::confirm_approval`), so adding
    even one file to a trusted bundle flips it to `changed` -- revoking a working Skill as a
    side effect of having an opinion about it.
    """
    proposals = os.path.abspath(skill_draft.PROPOSALS_DIR)
    skills = os.path.abspath(skill_draft.SKILLS_DIR)
    assert not proposals.startswith(skills + os.sep), (
        "proposals are written inside skills/, where they are discovered and digested")


def test_refusals_are_returned_rather_than_dropped(tmp_path):
    """A generator that emits only what it managed cannot be judged: nobody can tell a thin
    corpus from a generator that is quietly failing."""
    result = skill_draft.propose(ledger=str(tmp_path / "absent.jsonl"))
    assert set(result) >= {"new", "amend", "refused", "lesson_pairs", "candidates"}
    assert isinstance(result["refused"], list)
