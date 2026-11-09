# -*- coding: utf-8 -*-
"""Natural-language fan-out, end to end, without a browser (ledger row #212).

WHY THIS EXISTS. The owner's question was blunt: when a plain natural-language
goal is handed to the agent -- "clone a worth-looking harness repo, read it, and
propose an implementation" -- does fan-out actually FIRE, or is it inert? The
transcript that recorded the question (2026-09-08) answered that the mechanism,
wiring and flag were all present and the only thing missing was that the split
REQUEST was not reaching the worker; the open item was a verification that a
natural-language goal travels the whole decision path. This is that verification,
pinned as a regression so "is it just unable to" never has to be re-investigated
by hand.

WHAT IT CHECKS, AND WHAT IT DELIBERATELY DOES NOT. The live path is

    goal text
      -> splittability.judge()            (offline triage: SPLIT / UNCERTAIN / NO_SPLIT)
      -> Verdict.should_split             (only SPLIT sends the split request)
      -> fanout.SPLIT_JOB                  (the request the worker actually sends)
      -> agent reply
      -> fanout.declined_split / fanout_ready / subtasks_from
      -> fanout.child_goals               (the parallel children the fleet admits)

Every hop above is a pure function, so the whole chain is exercised here with no
Copilot tab and no network -- the agent's reply to SPLIT_JOB is supplied as the
text a real agent returns. The one thing only a live run can establish (that a
real model, handed SPLIT_JOB, replies in this shape) is out of scope by
construction and is what bench/companionbench's live adapter is for; this fixes
the PARSE-and-ROUTE contract that sits between the model and the fleet.

THE POINT ABOUT "clone -> analyse -> propose" IS THAT IT MUST NOT SPLIT. That one
natural sentence is sequentially dependent -- you cannot analyse before you clone
-- so NO_SPLIT is the correct answer, not a failure of fan-out. Fan-out firing
here would be the bug. The capability is proven by the genuinely independent
shape (several named repositories, one lookup each), which is the case the owner
was actually trying to reach.
"""
from __future__ import annotations

from relay import fanout as F
from relay import splittability as S


# A real owner-style request: several named repositories, each cloned/analysed
# independently. This is the shape that SHOULD fan out -- no shared state, no
# ordering between the repositories.
INDEPENDENT_GOAL = (
    "以下のリポジトリについて1件ずつcloneして中身を解析し、"
    "それぞれ実装案を出してください。"
    "【tencent/AngelSlim, deepseek-ai/DeepSeek-V3, openai/swarm, microsoft/autogen】"
)

# The owner's own phrasing of the single task: clone one repo, read it, propose.
# Sequentially dependent -> must NOT split.
SEQUENTIAL_GOAL = (
    "tencentのような見る価値のあるハーネス設計のリポジトリを一つ選び、"
    "cloneして中身を解析し、実装案を出してください。"
)


def _agent_split_reply(*repos):
    """The reply a worker returns to SPLIT_JOB: one numbered subtask per repo,
    terminated by the SUBTASKS_READY sentinel exactly as the prompt asks."""
    lines = ["以下のサブタスクに分割します。"]
    for i, repo in enumerate(repos, 1):
        lines.append("%d. %s をcloneし、ハーネス設計を解析して実装案を出す" % (i, repo))
    lines.append(F.SUBTASKS_READY)
    return "\n".join(lines) + "\n"


def test_independent_natural_language_goal_fans_out_end_to_end():
    """The whole chain for a genuinely independent natural-language goal."""
    # 1. The offline triage says SPLIT, and should_split -- the only verdict that
    #    sends the request -- is True.
    verdict = S.judge(INDEPENDENT_GOAL)
    assert verdict.decision == S.SPLIT, verdict.reason
    assert verdict.should_split is True

    # 2. The worker sends SPLIT_JOB; the agent answers in the shape the prompt asks
    #    for. Neither a decline nor an unfinished proposal.
    repos = ["tencent/AngelSlim", "deepseek-ai/DeepSeek-V3",
             "openai/swarm", "microsoft/autogen"]
    reply = _agent_split_reply(*repos)
    assert F.declined_split(reply) is False
    assert F.fanout_ready(reply) is True

    # 3. The reply parses to exactly the four subtasks -- not three, not thirteen.
    steps = F.subtasks_from(reply)
    assert len(steps) == len(repos)
    for repo, step in zip(repos, steps):
        assert repo in step

    # 4. The children the fleet would actually admit: one per step, each carrying
    #    the parent goal as context and its own 1/N scope.
    children = F.child_goals(INDEPENDENT_GOAL, steps)
    assert len(children) == len(repos)
    for i, child in enumerate(children, 1):
        assert child["subtask_index"] == i
        # The parent goal travels with every child (it is the specification), and
        # the child's own slice names the repo it owns.
        assert INDEPENDENT_GOAL.split("。")[0] in child["text"]
        assert repos[i - 1] in child["text"]
        # Every child of one split shares one campaign id.
        assert child["campaign_id"] == children[0]["campaign_id"]


def test_clone_analyse_propose_is_sequential_and_does_not_split():
    """The owner's single natural sentence must be judged NO_SPLIT.

    clone -> analyse -> propose cannot be parallelised (you cannot analyse what is
    not yet cloned), so NOT splitting it is the correct behaviour. This is the
    half of the answer that is easy to misread as "fan-out is broken".
    """
    verdict = S.judge(SEQUENTIAL_GOAL)
    assert verdict.decision == S.NO_SPLIT, verdict.reason
    assert verdict.should_split is False


def test_a_decline_reply_yields_no_children():
    """Even on a SPLIT goal, an agent that answers NO_SPLIT ends the split.

    The agent is the final judge: a triage of SPLIT only buys the question one
    turn, and a worker that replies with the decline marker produces no children.
    """
    decline = ("これは逐次依存の単一調査なので分割しません。\n"
               + F.NO_SPLIT_MARKER + "\n")
    assert F.declined_split(decline) is True
    assert F.subtasks_from(decline) == []


def test_should_split_is_the_gate_not_the_raw_decision():
    """UNCERTAIN must not route as permission to split.

    A natural-language goal that names independent-sounding parts but proves
    nothing ("別々に ... 分析") is UNCERTAIN, and UNCERTAIN resolves to should_split
    False -- the live path then hands it to the agent rather than fanning it out
    unasked. This pins that an UNCERTAIN verdict can never be silently treated as
    a SPLIT by a future edit.
    """
    goal = ("このリポジトリのハーネス設計を調べてください。別々に、"
            "アーキテクチャ、テスト戦略、依存関係の3観点でそれぞれ独立に分析して報告してください。")
    verdict = S.judge(goal)
    assert verdict.decision == S.UNCERTAIN, verdict.reason
    assert verdict.should_split is False
