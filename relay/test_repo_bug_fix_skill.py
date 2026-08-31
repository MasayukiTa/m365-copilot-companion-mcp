# -*- coding: utf-8 -*-
"""The bug-fix procedure as a Skill, and what still stands between it and being used.

WHAT THIS SKILL IS FOR. The procedure it holds -- interface-first, reproduce-first, implement
to the contract -- was written into bench/pro_stage_goals.py's goal builder, one numbered step
per observed failure, and grew 0 -> 1,590 -> 2,295 bytes across two commits. A goal is the
instruction for one task; it is not where accumulated knowledge belongs, because knowledge that
lives in an instruction is copied into every instruction whether or not it applies.

THE INLINE COPY IS DELIBERATELY STILL THERE. Removing it now would strip the procedure from
every benchmark worker, because this Skill cannot be reached yet:

  1. It is UNTRUSTED. Approval is a human action from the chat CLI, and nothing an agent does
     can request it -- so a Skill this server wrote is invisible to match() until the operator
     approves it.
  2. Even trusted it would LOSE. Measured 2026-08-31 on the live store, for a real SWE goal:

         skill_match("You are fixing a real bug in the open-source project **ansible/...")
             -> delegation-commander   score 1.0     (personal scope, ~/.claude/skills)
         best unapproved                -> repo-bug-fix   score 0.433

     SkillStore.discover() walks the personal library as well as the project's, and personal
     wins collisions. delegation-commander is a Claude Code playbook whose description says to
     use it for ALL coding tasks; handed to a fleet worker it prescribes dispatching the work
     to subagents that worker does not have.

So the migration is prepared and NOT completed, and both remaining steps are the operator's:
approve this Skill, and decide whether this server's store should serve the personal scope at
all. Cutting the goal text before then would trade a working procedure for a wrong one.
"""
import os

import pytest

from relay.skills import SkillStore, _match_tokens

SKILL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "repo-bug-fix")

#: `skills/` IS GITIGNORED, deliberately -- the bundles hold business content and must not
#: reach a public repository. So the Skill itself is not in this checkout's history, and every
#: test that reads it skips where it is absent rather than failing. A fresh clone has no
#: Skills at all; that is the intended state, not a broken one.
#:
#: The last test is different: it reads bench/, which IS tracked, and guards the sequencing.
#: It must run everywhere.
skill_present = pytest.mark.skipif(
    not os.path.isdir(SKILL_DIR),
    reason="skills/ is gitignored; this Skill is not present in this checkout")

#: A real goal from .fleet/transcripts, trimmed to its opening.
SWE_GOAL = (
    "You are fixing a real bug in the open-source project **ansible/ansible** "
    "(language: python). The repository is checked out locally at: C:/w/p05 . "
    "Read and edit the source with the file tools. Fix ONLY the source to resolve the "
    "issue; do NOT edit test files.")


def _skill():
    from relay.skills import load_bundle
    return load_bundle(SKILL_DIR, "project")


@skill_present
def test_the_bundle_loads():
    """A SKILL.md with, say, an unquoted ':' in its description is invalid YAML, and the
    Skill then simply never appears -- no error, nothing to debug."""
    st = SkillStore(os.path.dirname(os.path.dirname(SKILL_DIR)))
    assert "repo-bug-fix" not in (st.invalid_bundles() or {})
    assert _skill().name == "repo-bug-fix"


@skill_present
def test_its_description_overlaps_a_real_goal_enough_to_be_considered():
    """Matching needs MIN_MATCH_TOKENS shared terms. A Skill whose description does not speak
    the goal's vocabulary is never even scored -- which is how a correct procedure sits unused."""
    terms = _match_tokens(_skill().description)
    overlap = _match_tokens(SWE_GOAL) & terms
    assert len(overlap) >= SkillStore.MIN_MATCH_TOKENS, \
        "only %d shared tokens: %s" % (len(overlap), sorted(overlap))


@skill_present
def test_it_speaks_both_languages():
    """The benchmark goals are English and the operator's own work is Japanese. A description
    in one language is unreachable from the other."""
    terms = _match_tokens(_skill().description)
    assert _match_tokens("fixing a bug in a repository issue tests") & terms
    assert _match_tokens("リポジトリ バグ 修正 課題") & terms


@skill_present
def test_the_procedure_still_carries_the_evidence_that_produced_it():
    """The numbered steps are worth less than the measurements behind them. A procedure with
    the reasons stripped is one nobody can argue with or correct."""
    body = open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8").read()
    for marker in ("REPRODUCE-FIRST", "INTERFACE-FIRST", "実測"):
        assert marker in body, "%s is missing from the procedure" % marker


def test_the_goal_builder_still_carries_the_procedure_itself():
    """THE SEQUENCING GUARD. This Skill is not reachable yet -- untrusted, and outscored by a
    personal-scope Skill. Removing the inline copy before both are resolved would hand every
    benchmark worker no procedure at all, or the wrong one. When the migration completes, this
    test is what should be updated, deliberately, rather than discovered."""
    from bench import pro_stage_goals as G
    inst = sorted(G.BY_ID)[0]
    text = G.goal(inst, "C:/w/p00")["text"]
    for step in ("INTERFACE-FIRST", "REPRODUCE-FIRST"):
        assert step in text, "the goal lost %s before the Skill could replace it" % step
