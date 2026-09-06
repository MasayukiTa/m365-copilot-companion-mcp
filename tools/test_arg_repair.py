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


def test_empty_arguments_are_not_rewritten_when_the_tool_can_run_on_them():
    """Empty arguments are still never REMAPPED -- there is nothing to move. What changed is
    that "no wrong names" no longer implies "callable": a tool with an unfilled required
    parameter is explained instead of being handed to fn(**{}) to die on a bare TypeError."""
    def all_optional(start_line: int = 1, end_line: int = 0) -> str:
        return "ok"

    got = R.repair(all_optional, {}, name="all_optional", read_only=True)
    assert got["action"] == R.RUN and got["arguments"] == {} and not got["message"]


def test_empty_arguments_on_a_tool_with_a_required_parameter_are_explained():
    """read_file(path) cannot run on {}. This returned RUN before, and the caller's reward was
    "missing 1 required positional argument" with no statement of the accepted form."""
    got = R.repair(read_file, {}, name="read_file", read_only=True)
    assert got["action"] == R.EXPLAIN, got
    assert "path" in got["message"]


@pytest.mark.parametrize("guess", ["query", "task", "keywords", "scenario"])
def test_every_name_agents_actually_guessed_for_the_skill_lookup(guess):
    """All four appear in the ledger against skill_match."""
    got = R.repair(skill_match, {guess: "x"}, name="skill_match", read_only=True)
    assert got["action"] == R.REMAPPED and got["arguments"] == {"text": "x"}


# ── the gateway's own envelope, arriving one level too deep ───────────────────────────────────

def _unlock_like(password: str) -> str:
    """Stand-in with the real unlock's shape: one required parameter, mutating."""
    return "unlocked:" + password


def test_the_gateway_envelope_is_unwrapped_even_for_a_mutating_tool():
    """THE CALL THAT LOCKS A CALLER OUT OF THE SERVER.

    call_tool(name=..., arguments={...}) is the documented form, so a caller that forwards its
    own parameters verbatim arrives as {"name": ..., "arguments": {...}} with the real payload
    intact one wrapper out. Measured in .fleet/tool_events.jsonl: unlock was called exactly this
    way, with the correct password inside the envelope, and refused. Every mutating tool is
    gated behind unlock, so that one refusal ends the conversation's ability to do anything.

    Unlike the remap below this is not a guess -- the keys are the gateway's own two parameter
    names and the payload is a dict -- so it is unwrapped for mutating tools too.
    """
    plan = R.repair(_unlock_like, {"name": "unlock", "arguments": {"password": "s3cret"}},
                  name="unlock", read_only=False)
    assert plan["action"] == R.RUN, plan
    assert plan["arguments"] == {"password": "s3cret"}
    assert _unlock_like(**plan["arguments"]) == "unlocked:s3cret"


def test_an_envelope_naming_a_different_tool_is_not_unwrapped():
    """The name inside the envelope must agree with the tool being dispatched. If it does not,
    the caller meant something else and unwrapping would run this tool on another's payload."""
    plan = R.repair(_unlock_like, {"name": "shell_exec", "arguments": {"password": "s3cret"}},
                  name="unlock", read_only=False)
    assert plan["action"] == R.EXPLAIN, plan


def test_a_tool_that_really_takes_name_and_arguments_is_left_alone():
    """The guard that keeps the unwrap from eating a legitimate signature."""
    def gateway_like(name=None, arguments=None):
        return (name, arguments)

    plan = R.repair(gateway_like, {"name": "x", "arguments": {"a": 1}},
                  name="gateway_like", read_only=True)
    assert plan["action"] == R.RUN
    assert plan["arguments"] == {"name": "x", "arguments": {"a": 1}}, (
        "a tool whose own parameters are name/arguments was unwrapped out from under itself")


# ── nothing unexpected is not the same as callable ────────────────────────────────────────────

def test_a_call_missing_a_required_argument_is_explained_not_run():
    """`{}` has no wrong names in it, so this returned RUN and the call reached fn(**{}) and
    died on a bare "missing 1 required positional argument". 26 calls ended that way."""
    plan = R.repair(_unlock_like, {}, name="unlock", read_only=False)
    assert plan["action"] == R.EXPLAIN, plan
    assert "password" in plan["message"]
    assert "nothing was run" in plan["message"]


def test_the_name_only_envelope_asks_for_what_is_missing():
    """{"name": "unlock"} unwraps to {} -- there is genuinely no password in it. The caller
    should be told what to supply, not handed a TypeError."""
    plan = R.repair(_unlock_like, {"name": "unlock"}, name="unlock", read_only=False)
    assert plan["action"] == R.EXPLAIN, plan
    assert "missing required 'password'" in plan["message"]


def test_a_satisfied_call_still_runs_untouched():
    """The regression guard: the new required-argument check must not block correct calls."""
    plan = R.repair(_unlock_like, {"password": "s3cret"}, name="unlock", read_only=False)
    assert plan["action"] == R.RUN
    assert plan["arguments"] == {"password": "s3cret"}
