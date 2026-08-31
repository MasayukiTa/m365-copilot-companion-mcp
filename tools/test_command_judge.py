"""The judgement layer, tested without a model.

Every test here supplies its own `judge` callable. The module has no transport of its own for
exactly this reason: a test that could reach a model would be slow, flaky, and would sometimes
pass for reasons unrelated to the policy.
"""
import json

import pytest

from tools import command_judge as J


# ── what the judge is allowed to see ──────────────────────────────────────────────────────────

def test_the_request_excludes_the_agents_own_words():
    """The exclusion is the design, not an oversight.

    An agent that has talked itself into a command must not be able to hand that reasoning to
    its judge. This repository measured what self-report is worth one level up on the same day:
    workers reported DONE having edited nothing.
    """
    req = J.build_request("rm -rf build", cwd="C:/w/proj", user_messages=["fix the build"],
                          prior_commands=["pytest -x"])
    blob = json.dumps(req)
    assert "pending_command" in req and "user_messages" in req
    for forbidden in ("assistant", "rationale", "justification", "tool_result", "stdout"):
        assert forbidden not in blob


def test_prior_commands_are_included_but_their_output_is_not():
    req = J.build_request("git push --force", cwd="C:/w", prior_commands=["git status"])
    assert req["prior_commands"] == ["git status"]
    assert "results" not in req and "output" not in req


def test_the_request_says_whether_the_target_is_inside_the_workspace():
    inside = J.build_request("rm x", cwd="C:/w/proj/sub", workspace_root="C:/w/proj")
    outside = J.build_request("rm x", cwd="C:/other", workspace_root="C:/w/proj")
    unknown = J.build_request("rm x", cwd="C:/w/proj")
    assert inside["inside_workspace"] is True
    assert outside["inside_workspace"] is False
    assert unknown["inside_workspace"] is None


def test_history_is_bounded_so_one_long_session_cannot_crowd_out_the_command():
    req = J.build_request("x", cwd="C:/w",
                          user_messages=["m%d" % i for i in range(50)],
                          prior_commands=["c%d" % i for i in range(50)])
    assert len(req["user_messages"]) == 6
    assert len(req["prior_commands"]) == 10
    assert req["user_messages"][-1] == "m49"      # the RECENT ones, not the first six


def test_the_prompt_tells_the_judge_the_command_is_data():
    """The command is attacker-shaped. A judge that reads it as instructions is instructable."""
    p = J.SYSTEM_PROMPT
    assert "no authority" in p
    assert "NOT given the agent's explanation" in p
    assert "NOT given any command output" in p


# ── reading the answer ────────────────────────────────────────────────────────────────────────

def test_a_well_formed_verdict_is_read():
    v = J.parse_verdict('{"decision":"ALLOW","categories":["destructive"],"reason":"ok"}')
    assert v["decision"] == "ALLOW"
    assert v["categories"] == ["destructive"]


def test_fenced_and_chatty_answers_are_tolerated():
    """Models add prose and fences. Failing over formatting would make this layer fail for a
    reason that has nothing to do with safety."""
    v = J.parse_verdict('Sure!\n```json\n{"decision":"BLOCK_AND_RETRY","reason":"r"}\n```\n')
    assert v["decision"] == "BLOCK_AND_RETRY"


@pytest.mark.parametrize("raw", ["", "   ", "no json here",
                                 '{"decision":"MAYBE"}', '{"nope":1}', "[1,2]", "{oops"])
def test_anything_that_is_not_a_verdict_raises_rather_than_defaulting(raw):
    with pytest.raises(J.JudgeUnavailable):
        J.parse_verdict(raw)


def test_unknown_categories_are_dropped_not_rejected():
    v = J.parse_verdict('{"decision":"ALLOW","categories":["destructive","banana"],"reason":""}')
    assert v["categories"] == ["destructive"]


# ── the decision ──────────────────────────────────────────────────────────────────────────────

def test_a_verdict_is_returned_as_given():
    out = J.judge_command({}, lambda _b: '{"decision":"ALLOW","reason":"fine"}')
    assert out["decision"] == "ALLOW" and out["source"] == "judge"


def test_an_unreachable_judge_asks_a_human_rather_than_allowing():
    """A gate whose failure mode is 'carry on' protects nothing on exactly the days it is
    needed."""
    def _boom(_b):
        raise OSError("connection refused")
    out = J.judge_command({}, _boom)
    assert out["decision"] == J.REQUIRE_HUMAN
    assert out["source"] == "unavailable"


def test_an_unparseable_answer_asks_a_human_rather_than_allowing():
    out = J.judge_command({}, lambda _b: "I think it's probably fine?")
    assert out["decision"] == J.REQUIRE_HUMAN
    assert out["source"] == "unavailable"


def test_no_judge_configured_is_not_an_allow():
    out = J.judge_command({}, None)
    assert out["decision"] == J.REQUIRE_HUMAN
    assert out["source"] == "no_judge"


def test_the_judge_never_raises_into_its_caller():
    """It sits in front of every command. An exception here would break execution outright."""
    for bad in (lambda _b: (_ for _ in ()).throw(ValueError("x")),
                lambda _b: None,
                lambda _b: 12345):
        out = J.judge_command({}, bad)
        assert out["decision"] in (J.ALLOW, J.BLOCK_AND_RETRY, J.REQUIRE_HUMAN)


# ── what the outcome means for execution ──────────────────────────────────────────────────────

def test_allow_runs_and_block_does_not():
    assert J.outcome_blocks_execution({"decision": J.ALLOW}) is False
    assert J.outcome_blocks_execution({"decision": J.BLOCK_AND_RETRY}) is True


def test_require_human_with_nobody_to_ask_is_a_refusal():
    """An unattended run that reads 'ask someone' as 'go ahead' has inverted the point."""
    assert J.outcome_blocks_execution({"decision": J.REQUIRE_HUMAN}, human_available=False) is True
    assert J.outcome_blocks_execution({"decision": J.REQUIRE_HUMAN}, human_available=True) is False


def test_an_unrecognised_decision_blocks():
    assert J.outcome_blocks_execution({"decision": "PROBABLY"}) is True
    assert J.outcome_blocks_execution({}) is True
