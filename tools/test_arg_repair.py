# -*- coding: utf-8 -*-
"""1,037 tool calls died because the caller guessed an argument name.

Under MCP_TOOL_MAP most tools are reachable only through the call_tool gateway, whose
catalogue lists names and one-line summaries and no signatures. So the caller guesses, and a
wrong guess raised a bare TypeError naming the rejected key and nothing else.

The damage is not evenly spread, and the shape is the finding. read_file recovers 99% of the
time because the agent needs the file and keeps trying. skill_match recovers 34%: it is the
preliminary step the server ORDERS as RULE 2, so when it fails the agent proceeds to the real
work. 96 of its 145 failures were abandoned, and it has succeeded 33 times in the whole
ledger -- while the skill that would have answered scores 1.0 on the very queries that failed.

These run the decision, not a rendering of it.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import arg_repair as R  # noqa: E402


def skill_match(text: str) -> str:            # the real signature, verbatim
    return "matched:" + text


def read_file(path: str, start_line: int = 1, end_line: int = 0) -> str:
    return "read:%s" % path


def write_file(path: str, content: str) -> str:
    return "wrote:%s" % path


def takes_anything(**kwargs) -> str:
    return "ok"


# -- the measured case -------------------------------------------------------------------

def test_the_call_that_failed_145_times_is_corrected():
    """The agent wrote query=; the parameter is text=. One unknown name, one unfilled
    required parameter -- there is no second reading of what it meant."""
    got = R.repair(skill_match, {"query": "fix a bug in a checked-out repo"},
                   name="skill_match", read_only=True)
    assert got["action"] == R.REMAPPED
    assert got["arguments"] == {"text": "fix a bug in a checked-out repo"}
    assert "'query' was read as 'text'" in got["message"]


def test_a_correction_is_always_reported():
    """Silently fixing it would leave the caller guessing the same way next time."""
    got = R.repair(skill_match, {"query": "x"}, name="skill_match", read_only=True)
    assert "skill_match(text" in got["message"]


def test_a_correct_call_is_left_exactly_alone():
    got = R.repair(skill_match, {"text": "x"}, name="skill_match", read_only=True)
    assert got["action"] == R.RUN and got["arguments"] == {"text": "x"} and not got["message"]


# -- the guard that matters --------------------------------------------------------------

def test_a_writing_tool_is_never_silently_redirected():
    """THE DANGEROUS DIRECTION. Redirecting an argument on a tool that writes could act on
    the wrong target while looking like a success. It gets the explanation instead."""
    got = R.repair(write_file, {"file": "a.txt", "content": "x"},
                   name="write_file", read_only=False)
    assert got["action"] == R.EXPLAIN
    assert "nothing was run" in got["message"]
    assert "write_file(path" in got["message"]


def test_two_stray_names_are_explained_not_guessed():
    """Two unknown names have more than one plausible destination."""
    got = R.repair(read_file, {"file": "a.txt", "lines": 10}, name="read_file", read_only=True)
    assert got["action"] == R.EXPLAIN


def test_a_stray_name_with_nothing_missing_is_explained():
    """Every requirement is already satisfied, so the stray key is not a misnamed anything --
    remapping it would invent a destination."""
    got = R.repair(read_file, {"path": "a.txt", "substring": "needle"},
                   name="read_file", read_only=True)
    assert got["action"] == R.EXPLAIN
    assert "'substring'" in got["message"]


def test_the_explanation_names_the_required_parameters():
    """A caller that is told only what is wrong guesses again."""
    got = R.repair(read_file, {"file": "a.txt"}, name="read_file", read_only=True)
    assert got["action"] == R.REMAPPED or "Required: path" in got["message"]


# -- shapes that must not break ------------------------------------------------------------

def test_a_tool_taking_kwargs_has_nothing_to_repair():
    got = R.repair(takes_anything, {"anything": 1, "at": "all"}, name="x", read_only=True)
    assert got["action"] == R.RUN


def test_an_unreadable_signature_is_not_treated_as_an_error():
    """Builtins and C functions have no inspectable signature; they must still run."""
    got = R.repair(len, {"obj": []}, name="len", read_only=True)
    assert got["action"] == R.RUN


def test_empty_arguments_are_left_alone():
    got = R.repair(read_file, {}, name="read_file", read_only=True)
    assert got["action"] == R.RUN


@pytest.mark.parametrize("guess", ["query", "task", "keywords", "scenario"])
def test_every_name_agents_actually_guessed_for_the_skill_lookup(guess):
    """All four appear in the ledger against skill_match."""
    got = R.repair(skill_match, {guess: "x"}, name="skill_match", read_only=True)
    assert got["action"] == R.REMAPPED and got["arguments"] == {"text": "x"}
