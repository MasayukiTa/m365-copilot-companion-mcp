# -*- coding: utf-8 -*-
"""The server instructions must stay a set of RULES, never a growing pile of cases.

THE QUESTION THIS ANSWERS. "この事例を追加してシステムプロンプトとする件、これは今後増加する
ことを考えられていない。skills や memory にあるべき。" It is the right objection: a prompt that
grows by one paragraph per situation encountered has no bound, and every token of it is paid on
every single call, forever, whether or not the situation ever recurs.

THE ARCHITECTURE IS ALREADY RIGHT. RULE 2 of the instructions tells the agent to call
skill_match before any domain work and skill_load on a confident trusted match. Procedures live
in skills/ and facts live in memory; the prompt says how to find them and carries none of them.

WHAT WAS MISSING WAS ANYTHING THAT KEEPS IT RIGHT. An intention decays one well-meaning append
at a time, and each append looks reasonable on its own -- the whole failure mode is that no
single one is the problem. These tests make the boundary something that fails loudly instead.

MEASURED WHEN WRITTEN (2026-09-01): instructions 4281 characters; five trusted skills; in the
preceding hour every skill consultation matched (4 of 4, all repo-bug-fix) and three loads
followed. The delegation works, so there is no excuse for a case to be inlined instead.
"""
import ast
import io
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A ceiling, not a target. It is roughly 40% above the size at which this was written, so
#: ordinary rewording never trips it and appending a case or two does. When a change genuinely
#: needs more room, RAISE IT DELIBERATELY in the same commit that needs it -- that edit is the
#: point at which someone has to justify what they added.
MAX_INSTRUCTIONS_CHARS = 6000


def instructions_text():
    tree = ast.parse(io.open(os.path.join(REPO, "main.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "instructions":
            return ast.literal_eval(node.value)
    raise AssertionError("main.py no longer passes instructions= to the server")


def skill_names():
    root = os.path.join(REPO, "skills")
    if not os.path.isdir(root):
        return []
    return [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]


def test_the_instructions_stay_within_their_budget():
    """Every token here is paid on every call forever. A case that recurs twice a year is not
    worth that; a skill that is loaded when it matches is."""
    text = instructions_text()
    assert len(text) <= MAX_INSTRUCTIONS_CHARS, (
        "the instructions are %d characters, over the %d budget. If a case was appended, it "
        "belongs in skills/ or memory. If the rules genuinely grew, raise the budget in this "
        "same commit and say why." % (len(text), MAX_INSTRUCTIONS_CHARS))


def test_the_instructions_name_no_individual_skill():
    """THE SHARPEST FORM OF THE RULE. The prompt says how to FIND a procedure; it never carries
    one. A case being inlined almost always shows up as its domain appearing here by name, so
    naming a skill is the tell.

    skill_match / skill_load / skill_list are the mechanism and are expected."""
    text = instructions_text()
    named = [n for n in skill_names() if n in text]
    assert not named, (
        "the instructions name %s. A procedure belongs in its skill, and the prompt should "
        "only tell the agent to look it up." % named)


def test_the_delegation_rule_is_still_there():
    """The budget above is only safe BECAUSE the lookup path exists. If skill_match stopped
    being required, keeping the prompt small would just mean losing the knowledge."""
    text = instructions_text()
    assert "skill_match" in text
    assert "skill_load" in text
    assert "agent_memory_save" in text or "memory" in text


def test_trust_is_stated_as_not_granting_execution_rights():
    """A Skill is instructions, not authority. If loading one could widen what may be executed,
    the cheapest attack on this system would be to get a Skill approved."""
    text = instructions_text()
    assert "never grants extra execution rights" in text or "keeps its normal unlock" in text


@pytest.mark.parametrize("marker", ["For example", "たとえば", "例:", "e.g. when the user asks",
                                    "In the case where", "この事例"])
def test_no_worked_example_has_crept_in(marker):
    """Worked examples are how a rule set turns into a case pile. They read as helpful, they
    are individually small, and there is no natural place for them to stop."""
    assert marker not in instructions_text()


def test_the_skills_the_prompt_delegates_to_actually_exist():
    """A rule pointing at an empty store is worse than an inlined case: the agent looks, finds
    nothing, concludes there is no procedure, and invents one. That exact failure is recorded
    in skill_ops -- six Skills sat unreadable for weeks while callers re-derived their work."""
    assert skill_names(), "the prompt delegates to skills/ and there are none"


def test_the_rules_are_numbered_once_each_and_in_order():
    """Inserting a rule means renumbering the ones after it, and a duplicate number is the
    natural mistake. It happened while adding RULE 3: two rules ended up numbered 7."""
    import re
    nums = [int(n) for n in re.findall(r"RULE (\d+)", instructions_text())]
    assert nums == sorted(set(nums)), "rule numbers repeat or are out of order: %s" % nums
    assert nums == list(range(1, len(nums) + 1)), "rule numbers are not 1..N: %s" % nums


def test_the_instructions_point_work_at_the_fleet():
    """MEASURED 2026-09-06. The owner sent two ordinary requests from a phone. The agent obeyed
    RULE 1, then tried to do the work itself: write_file and run_python refused for want of an
    unlock token, .env was read three times looking for the password and refused each time, and
    it fell back to wandering the repo read-only. fleet_submit was never called -- one of 175
    names in a catalogue, with nothing saying it was the answer. Neither request reached the
    fleet, and the door had been measured working days earlier.

    This pins the instruction, not the outcome: whether the agent then obeys is a separate
    question, answered by whether a submission actually arrives."""
    text = instructions_text()
    assert "fleet_submit" in text, (
        "nothing tells the agent to hand work to the fleet; a door nobody is told about is a "
        "door nobody uses")
