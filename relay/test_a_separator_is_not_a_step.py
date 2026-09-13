# -*- coding: utf-8 -*-
"""A horizontal rule counted as a plan step, and the cleaner did not do what it said.

FOUND while verifying the fan-out repair against the run it was written for. `extract_plan` on
the real turn-1 reply of `r6aa597a8_a0` returned FOURTEEN steps, and the first of them was:

    1  --

`_STEP_RE` accepts a `-` bullet, so a markdown rule matches it. `_clean_step("---")` returned
`"--"` -- non-empty, so it survived the caller's `if step:` guard and became a step whose entire
text is punctuation. `***` was dropped only because `.strip("*")` happens to empty it: an
accident, not a rule.

WHAT IT COST HERE, AND WHAT IT COULD COST. Fourteen is over MAX_CHILDREN (12), so the whole
seven-way split was refused and a fifty-minute run did the job in one conversation. The rule was
not the only surplus in that reply -- six shared constraints were also counted -- so it was not
the sole cause. But on a reply with twelve real steps the separator alone is the entire
difference between a split and no split, and on a short one it dispatches a child whose goal
text is `--`.

AND THE SECOND DEFECT, IN THE SAME LINE. `_clean_step`'s docstring promised
`'**calc.py を読む**:' -> 'calc.py を読む'` and did not deliver it: emphasis was stripped BEFORE
the colon, so `strip("*")` saw a string ending in `:`, removed only the leading pair, and the
colon came off afterwards with nothing left to re-strip. Every bolded step that ended in a colon
carried a trailing `**` into the goal text handed to a child. The function is applied to a
fixpoint now, and the example in its docstring is a case below.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout as F  # noqa: E402
from relay.planner import _clean_step, extract_plan  # noqa: E402

TRANSCRIPT = os.path.join(REPO, ".fleet", "transcripts", "r6aa597a8_a0_w0.jsonl")


# ── a separator is not a step ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rule", ["---", "--", "----------", "* * *", "***", "  ---  ", "- --"])
def test_a_horizontal_rule_is_not_a_step(rule):
    assert _clean_step(rule) == "", rule


@pytest.mark.parametrize("arrow", ["→", "…", "::", "()"])
def test_punctuation_alone_is_not_a_step(arrow):
    """The rule is "does this carry any content", not a list of separators somebody thought of."""
    assert _clean_step(arrow) == ""


def test_a_rule_between_steps_does_not_become_one():
    """THE DEFECT, at the level the caller sees. The count is what MAX_CHILDREN judges."""
    reply = "SUBTASKS_READY\n---\n1. 読む\n2. 直す\n---\n3. 試す\n"
    assert extract_plan(reply) == ["読む", "直す", "試す"]


def test_a_real_step_that_merely_contains_a_dash_survives():
    """A guard that fired on content would be worse than the defect it replaced."""
    assert _clean_step("ci.yml を読む -- 32KB") == "ci.yml を読む -- 32KB"
    assert _clean_step("1. a-b-c") == "1. a-b-c"


# ── the cleaner does what it says ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("src,want", [
    ("**calc.py を読む**:", "calc.py を読む"),   # the docstring's own example, which failed
    ("**実装する**:", "実装する"),
    ("**a**", "a"),
    ("計画を書く", "計画を書く"),
    ("**b**：", "b"),                            # full-width colon
])
def test_emphasis_and_colon_come_off_in_either_order(src, want):
    assert _clean_step(src) == want


def test_it_does_not_eat_a_colon_inside_a_step():
    """Only a TRAILING colon is punctuation; one in the middle is the sentence."""
    assert _clean_step("注意: ここは触らない") == "注意: ここは触らない"


# ── against the run it was found in ───────────────────────────────────────────────────────

def test_the_reply_that_was_refused_no_longer_carries_a_phantom():
    """A fixture proves what I think happened; the transcript proves what did. Skipped once
    retention has taken it, because an assertion that quietly stops running is worse."""
    if not os.path.isfile(TRANSCRIPT):
        pytest.skip("the transcript for r6aa597a8_a0 is no longer on this machine")
    rows = [json.loads(l) for l in io.open(TRANSCRIPT, encoding="utf-8", errors="replace")
            if l.strip()]
    turn1 = [r for r in rows if r.get("role") == "assistant" and r.get("turn") == 1][0]["text"]

    steps = extract_plan(turn1)
    assert not any(not any(c.isalnum() for c in s) for s in steps), steps
    # THIRTEEN, NOT FOURTEEN: the separator is gone and the six shared constraints remain --
    # still over MAX_CHILDREN, which is why the parse and not the bound was the defect.
    assert len(steps) == 13, len(steps)
    assert len(steps) > F.MAX_CHILDREN, "the old path would still have refused this"

    # And the path that actually runs now takes the LAST ascending run: the seven real subtasks.
    assert len(F.last_numbered_run(turn1)) == 7
    assert len(F.subtasks_from(turn1)) == 7
