# -*- coding: utf-8 -*-
"""Every goal carried a list of remembered themes, and the list was almost entirely noise.

MEASURED ON A REAL GOAL, 2026-09-14. Priming "9月分のGC付着異物の報告資料を作ってください" put
**999 characters** into the worker's prompt: ten index lines, of which NINE were arithmetic smoke
tests -- "5 と 6 を足した数だけを1行で返してください" and its siblings -- and the tenth was an
earlier failed run of the same work. The protocol those 999 characters sit next to is about
1,401. Not one line could have helped.

RANKING AND PRUNING WERE ALREADY THERE. `rank_index_lines` orders by overlap with the goal and
`prune_index_lines` drops the unrelated entries -- but only when NONE of them shares a token, so
a single line matching on "ogf" carried the other nine in with it. Each fix made the enumeration
survive one more round of growth. The store holds 216 themes, 36% of them one-shot questions,
because a theme is minted from the opening words of ANY goal; at ten thousand the question stops
being which forty to send.

THE OWNER'S INSTRUCTION IS THE DESIGN: "インデックスどころかそれが存在するパスだけ渡せばよい".
A worker that judges the past relevant reads it -- the same way it reaches a Skill, by asking
rather than by being handed a catalogue. One line, and it stays one line however large the store
grows.

WHAT THIS DOES NOT DO. It does not stop themes being minted from one-shot goals; the store still
fills. It stops that filling reaching every prompt.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pytest  # noqa: E402

from relay.project_memory import record_task, theme_from_goal  # noqa: E402
from relay.relay_fleet import _MEMORY_HEADER, _MEMORY_POINTER, _with_theme_memory  # noqa: E402


GOAL = "9月分のGC付着異物の報告資料を作ってください。前回の成果物は ...260902.pptx です。"


@pytest.fixture(autouse=True)
def _a_store_with_something_in_it():
    """conftest points the store at a per-run temp directory, so it is EMPTY -- and an empty
    store primes nothing, which would make every assertion below pass without the fix. Seed one
    theme plus the noise this is about, so the block under test actually gets built.

    The first draft skipped this and failed on "the worker is no longer told where the record
    is": the block was empty because there was no memory, not because the pointer was missing."""
    record_task(theme_from_goal(GOAL), GOAL, "DONE", note="9月分")
    for n in (2, 4, 6, 8):
        g = "%d と %d を足した数だけを1行で返してください" % (n, n + 1)
        record_task(theme_from_goal(g), g, "DONE")


def _injected(goal=GOAL):
    return _with_theme_memory(goal)[:-len(goal)]


# ── the property ──────────────────────────────────────────────────────────────────────────

def test_the_prompt_names_where_the_memory_is_and_does_not_quote_it():
    body = _injected()
    assert ".fleet/memory/" in body, "the worker is no longer told where the record is"
    assert _MEMORY_POINTER in body


def test_no_theme_titles_reach_the_prompt():
    """THE DEFECT. The titles ARE the goals people typed, so a catalogue of them is a catalogue
    of past instructions -- including the ones that were never worth remembering."""
    body = _injected()
    assert "](" not in body, "index lines are back in the prompt"
    assert "件 / 最終" not in body, "the per-theme summary is back in the prompt"
    assert "記憶している他のテーマ" not in body


def test_the_block_does_not_grow_with_the_store():
    """THE WHOLE POINT, asserted as a bound rather than as an observation. At 216 themes the
    old block was 999 characters; the objection was what happens at ten thousand."""
    assert len(_injected()) < 300, (
        "the primed block is %d characters -- it is carrying content again" % len(_injected()))


def test_a_goal_with_no_memory_at_all_is_left_alone():
    """A store with nothing for this theme must not gain a header announcing nothing."""
    goal = "ZZ-no-such-theme-" + "x" * 40
    out = _with_theme_memory(goal)
    assert out == goal or _MEMORY_HEADER not in out


def test_priming_twice_does_not_stack():
    """The body is rebuilt for replays and for token-limit recycles, and the guard is the
    header. If that stops working the prompt grows by a block per rebuild."""
    once = _with_theme_memory(GOAL)
    twice = _with_theme_memory(once)
    assert once == twice, "the memory block is being prepended more than once"


def test_the_repo_task_builder_points_the_same_way():
    """relay/code_task.py has its own prime for repository work. Fixing one and leaving the
    other is how a defect comes back through the door nobody looked at."""
    import io
    src = io.open(os.path.join(REPO, "relay", "code_task.py"), encoding="utf-8").read()
    i = src.index("このリポジトリでの過去の作業メモ")
    arm = src[max(0, i - 400):i + 300]
    assert "_MEMORY_POINTER" in arm, (
        "code_task still prints the memory contents into its prompt")
