# -*- coding: utf-8 -*-
"""Every continuation prompt asked for the one reply shape the parser cannot read.

`control_markers.parse()` takes the marker from the LAST NON-EMPTY LINE. That is deliberate,
and the comment above `_TRAILING` justifies widening the grammar on exactly this ground:

    the protocol prompt itself says 最後の行に DONE -- an agent that writes 作業完了。DONE is
    COMPLYING with it.

The protocol prompt did say that. The five CONTINUATION prompts did not. Every one of them said:

    完了したら DONE、無理なら FAIL と理由を書いてください。

-- no placement, and an invitation to write the reason AFTER the marker.

MEASURED on run r6aa597a8_a0. Turn 3 answered `FAIL（成果物のファイル保存のみ未達）` at character
428 of a 1,530-character reply and wrote 1,098 more characters of reasons after it. `parse()`
found no marker on the last line, so the reply fell to the MARKERLESS settle requirement -- more
samples, longer dwell -- which the turn budget did not allow. It timed out at 240 s and the full
7,890-character goal was re-sent, three more times. Turns 7 and 8 ended with `DONE` as their last
four characters and settled in 22 s and 100 s.

**The agent was complying with what it was asked.** The sentence was the defect, and it was the
same sentence copied five times, which is how it came to differ from the goal's own wording.
`CLOSING_INSTRUCTION` now lives in the module that parses its result.
"""
from __future__ import annotations

import ast
import io
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay.control_markers import CLOSING_INSTRUCTION, parse  # noqa: E402


# ── the instruction asks for a shape the parser accepts ───────────────────────────────────

@pytest.mark.parametrize("reply", [
    "調べました。\nDONE",
    "調べました。\nFAIL: 書き込み経路が全て閉じている",
    "調べました。\nFAIL",
    "理由を先に書きます。\n作業完了。DONE",
])
def test_a_reply_obeying_the_instruction_parses(reply):
    m = parse(reply)
    assert m is not None, reply
    assert m.kind in ("DONE", "FAIL")


@pytest.mark.parametrize("reply", [
    "FAIL（保存のみ未達）\n\n理由はこの後に長く続きます。",
    "FAIL: 理由\n\n補足が後ろに続く",
])
def test_the_shape_the_old_wording_invited_does_not_parse(reply):
    """THE DEFECT, as a fact about the two together. Kept as a test rather than a comment so
    that widening the parser later has to face what the instruction promises."""
    assert parse(reply) is None, reply


def test_the_instruction_says_where_the_marker_goes():
    """The clause the five copies had lost. The original goal carried it; they did not."""
    assert "最後の行" in CLOSING_INSTRUCTION, CLOSING_INSTRUCTION


def test_the_instruction_says_the_reason_comes_first():
    """A FAIL with no reason is not actionable, and a reason after the marker is not readable.
    Both halves are load-bearing, so both are checked."""
    assert "より前" in CLOSING_INSTRUCTION, CLOSING_INSTRUCTION


def test_it_names_both_outcomes():
    assert "DONE" in CLOSING_INSTRUCTION and "FAIL" in CLOSING_INSTRUCTION


# ── one sentence, not five ────────────────────────────────────────────────────────────────

def test_no_module_writes_its_own_copy():
    """Five copies is how the placement clause went missing from all of them while the goal
    that started the run still had it."""
    stale = []
    for pkg in ("relay", "tools", "bench", "bridge", "scripts"):
        root = os.path.join(REPO, pkg)
        if not os.path.isdir(root):
            continue
        for dirpath, _d, files in os.walk(root):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py") or fn.startswith("test_"):
                    continue
                path = os.path.join(dirpath, fn)
                src = io.open(path, encoding="utf-8", errors="replace").read()
                # STRING LITERALS ONLY, VIA THE AST. A raw substring search flagged
                # control_markers.py, which quotes the old wording in a comment to explain
                # what was wrong with it -- the same "a check that reads prose about a past
                # defect reports the defect as present" this burn-down keeps running into.
                # A prompt is a literal; an explanation is not.
                try:
                    tree = ast.parse(src)
                except Exception:
                    continue
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and "無理なら FAIL と理由を書いて" in node.value):
                        stale.append(os.path.relpath(path, REPO).replace("\\", "/"))
                        break
    assert not stale, "古い言い回しを自前で持っている: %s" % ", ".join(stale)


def test_the_prompt_sites_use_the_shared_one():
    """A site that stopped using it would drift silently -- the failure this replaced."""
    for rel in ("relay/relay_fleet.py", "relay/fleet_runner.py"):
        src = io.open(os.path.join(REPO, rel), encoding="utf-8").read()
        assert "CLOSING_INSTRUCTION" in src, rel


def test_every_use_is_concatenated_not_juxtaposed():
    """A BARE NAME CANNOT FOLLOW A STRING LITERAL. Swapping the literal for the constant
    turned five prompts into a SyntaxError, because implicit concatenation joins literals
    only. Parsing the module is the check; a substring search would not have caught it."""
    for rel in ("relay/relay_fleet.py", "relay/fleet_runner.py"):
        src = io.open(os.path.join(REPO, rel), encoding="utf-8").read()
        ast.parse(src)  # raises if a juxtaposition came back
