# -*- coding: utf-8 -*-
"""How a goal ENDS is not what it asks for.

`skill_draft` proposes a Skill when the record holds something quotable: a path, an output
contract, or a lesson. `DONE` and `最後の行に` were counted as output contracts. Both describe
how a fleet goal finishes -- `relay/control_markers.CLOSING_INSTRUCTION` asks every worker for a
final DONE line, and an operator writing a goal by hand mirrors it.

MEASURED over the 157 qualified candidates: 52% carried `DONE`, and 13% carried nothing else.
The sieve was passing those on the strength of the protocol's own boilerplate, and it showed in
the ranking -- the most-run "work" included an arithmetic probe (52 runs), a documents listing
(62) and a filename listing (40). Nobody needs a procedure for 137 + 486.

Dropping the two markers took the proposals from 95 to 57 and removed every probe. What remains
is the telephone-directory classification, the furigana check, the OGF report, the mail
searches: work somebody actually does.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import skill_draft  # noqa: E402

PROBE = "次の足し算の答えを数字だけで書いてください: 137 + 486。最後の行に DONE と書いてください。"
REAL = ("登録済みフリガナの正誤を判定する作業。【出力形式・厳守】1行1名、"
        "「氏名|セイ|メイ|OKまたはNG」の4列のみ。最後の行に DONE と書いてください。")


@pytest.mark.parametrize("mark", ["DONE", "最後の行に"])
def test_a_completion_marker_is_not_counted_as_a_contract(mark):
    assert mark not in skill_draft._CONTRACT_MARKS, (
        "%r is how a goal ends, not what it asks for; counting it passed every smoke probe"
        % mark)


def test_a_probe_declares_no_output_contract():
    """It ends like every other goal and asks for nothing about the shape of its answer."""
    assert skill_draft._contracts_in(PROBE) == []


def test_a_real_instruction_still_declares_one():
    """The point is not to tighten the sieve until nothing passes."""
    got = skill_draft._contracts_in(REAL)
    assert got, "a goal that names its columns and says 厳守 no longer counts as declaring one"
    assert "出力形式" in got and "厳守" in got


def test_the_closing_instruction_is_the_source_of_the_boilerplate():
    """If the protocol stops asking for a DONE line, this reasoning needs revisiting -- so the
    test points at the text rather than restating it."""
    from relay.control_markers import CLOSING_INSTRUCTION
    assert "DONE" in CLOSING_INSTRUCTION, (
        "the protocol no longer asks for a DONE line, so `DONE` in a goal may now be the "
        "operator's own choice; re-check whether it belongs in _CONTRACT_MARKS")


def test_a_goal_with_only_a_completion_marker_is_refused(tmp_path):
    """End to end: nothing quotable means no proposal, and the refusal says why."""
    import io
    import json
    led = tmp_path / "ledger.jsonl"
    with io.open(str(led), "w", encoding="utf-8") as fh:
        for i in range(5):
            fh.write(json.dumps({"event": "worker_done", "goal": PROBE, "outcome": "DONE",
                                 "ts": 1000.0 + i}, ensure_ascii=False) + "\n")
    # `propose` reads the real candidate ledger, so this checks the predicate it rests on
    # rather than re-plumbing the whole call: no path, no contract, and a probe has no lesson.
    assert not skill_draft._paths_in(PROBE)
    assert not skill_draft._contracts_in(PROBE)
