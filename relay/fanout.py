"""Split one long goal into independent sub-goals, run them, and put the answers back together.

WHY THIS EXISTS. Several goals in one day failed the same way, and none of them failed at the
work: they failed at trying to do all of it inside one conversation.

  * one exhausted the model's context after ten turns of real findings, and the findings went
    down with the conversation;
  * one reported "2〜4月の全件は膨大で、全件出力するとレスポンス上限を大幅に超える" and then
    ground through the quarter a week at a time until its conversation died at turn 16;
  * one said plainly that a single response holds 25 records and a full sweep cannot fit in
    one turn, which is not a complaint about the agent -- it is a description of the shape of
    the work.

A quarter of mail is not one task. It is thirteen weeks that happen to share a format, and
thirteen conversations would each finish comfortably. Everything needed to run them already
existed: the fleet adds goals mid-run through `add_box`, carries lineage on every worker
(task_id / parent_task_id / campaign_id / depth), and `planner.extract_plan` already reads a
numbered list out of an agent's reply. What was missing was a worker that produces sub-goals
and something that reads the children's answers back into one.

This module is the decision-making half, kept free of the browser and the fleet loop so the
rules below can be tested as rules: how a split is recognised, what a child is allowed to
inherit, how many children are too many, and what the parent is asked at the end.
"""
from __future__ import annotations

import hashlib
import re

from relay.planner import extract_plan

#: The agent writes this when its split is ready, mirroring PLAN_READY. A distinct marker,
#: because a split and a plan are different things: a plan is steps for ONE conversation to
#: work through in order, a split is work for SEVERAL conversations to do independently.
SUBTASKS_READY = "SUBTASKS_READY"

#: Upper bound on children from a single split. Not a resource limit -- the fleet's own
#: admission control handles that -- but a sanity bound: a "split" that produces sixty pieces
#: is a plan that was mis-parsed, or an agent listing every record it intends to fetch, and
#: turning that into sixty conversations would be a great deal of damage done quickly.
MAX_CHILDREN = 12

#: And a floor. One child is not a split, it is the same goal with extra steps -- accepting it
#: would let a goal bounce between "split" and "do it" without ever doing either.
MIN_CHILDREN = 2

#: How deep the tree may go. One level by default: children do the work, they do not split
#: again. Recursive splitting is the shape that turns one runaway goal into an unbounded
#: number of conversations, and nothing here needs it yet.
MAX_DEPTH = 1

#: A step shorter than this is a fragment ("2月", "続き") rather than an instruction that a
#: fresh conversation -- which will not have seen the parent's reasoning -- could act on.
MIN_STEP_CHARS = 8

SPLIT_JOB = (
    "【この依頼は分割して並列実行します】\n"
    "上記の目標を、**互いに独立して実行できる**サブタスクに分割してください。実行はまだしないでください。\n"
    "各サブタスクは次を満たすこと:\n"
    "  1. それ単独で、他のサブタスクの結果を見なくても完了できる\n"
    "  2. 1つの会話に収まる分量である（応答の上限に当たらない範囲に区切る）\n"
    "  3. 何を対象にするかが具体的に書かれている（期間・対象・出力先を明示。"
    "「残りを続ける」のような相対的な指示は不可 — 実行する側は今の会話を見ていません）\n"
    "  4. サブタスク同士で重複も抜けも無いこと\n"
    "%d〜%d 個に分割し、番号付きの箇条書きで列挙してください。"
    "最後の行に %s と書いてください。" % (MIN_CHILDREN, MAX_CHILDREN, SUBTASKS_READY)
)


def fanout_ready(resp) -> bool:
    """Has the agent finished proposing a split?"""
    return SUBTASKS_READY.upper() in (resp or "").upper()


def campaign_id_for(parent_goal) -> str:
    """A stable id for one parent and its children, derived from the goal itself.

    Derived rather than random because the fleet's scripts must not call Math.random's
    equivalents for ids that appear in a resumable run: the same goal resumed must land in
    the same campaign, or the children of the first attempt and the second become two
    unrelated families in the same status file.
    """
    return "c" + hashlib.sha256((parent_goal or "").encode("utf-8")).hexdigest()[:12]


def _dedupe(steps):
    """Drop repeats, keeping order. An agent that lists a step twice would otherwise get two
    conversations doing identical work and a merge that double-counts every row."""
    seen, out = set(), []
    for s in steps:
        key = re.sub(r"\s+", "", s)
        if key and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def subtasks_from(resp):
    """The sub-task list in an agent's split reply, or [] if it is not usable as one.

    Returning [] rather than a partial list is deliberate: a split that came back as one item,
    or as forty, is not a split this can act on, and guessing which half of it to believe is
    how a fan-out quietly runs the wrong work.
    """
    steps = [s.strip() for s in extract_plan(resp or "")]
    steps = _dedupe([s for s in steps if len(s) >= MIN_STEP_CHARS])
    if len(steps) < MIN_CHILDREN or len(steps) > MAX_CHILDREN:
        return []
    return steps


def child_goals(parent_goal, steps, *, parent_task_id="", campaign_id="", depth=0,
                checks=None, cwd=None):
    """Turn the accepted steps into goal items the fleet can admit.

    Each child carries the PARENT'S goal as context, not just its own step. A child runs in a
    conversation that has never seen the parent's: handed only "2月分を取得する" it does not
    know the format, the exclusions, or where the output belongs, and it will invent all
    three. The parent's instructions are the specification; the step says which part of it
    this conversation owns.
    """
    if depth >= MAX_DEPTH:
        return []
    cid = campaign_id or campaign_id_for(parent_goal)
    out = []
    for i, step in enumerate(steps, 1):
        text = (
            "%s\n\n"
            "【この会話が担当する範囲 — 全体の %d/%d】\n%s\n\n"
            "上の範囲だけを担当してください。他の範囲は別の会話が並行して担当しているので、"
            "手を出さないこと。担当範囲を完了したら、何を何件取得したかを明記して "
            "DONE と書いてください。"
            % (parent_goal, i, len(steps), step)
        )
        out.append({
            "text": text,
            "checks": checks,
            "cwd": cwd,
            "campaign_id": cid,
            "task_id": "%s-%d" % (cid, i),
            "role": "subtask",
            "parent_task_id": parent_task_id or cid,
            "depth": depth + 1,
            "subtask_index": i,
            "subtask_of": len(steps),
        })
    return out


def ready_to_aggregate(records):
    """Have all of a campaign's sub-tasks finished?

    `records` are that campaign's children: {"finished": bool, ...}. Empty means no children
    were ever admitted, which is not "ready" -- aggregating nothing would produce a confident
    summary of work that never ran.
    """
    return bool(records) and all(r.get("finished") for r in records)


def aggregation_goal(parent_goal, records, *, campaign_id="", parent_task_id="",
                     limit_each=1200):
    """The goal item that merges a finished campaign.

    A goal rather than a turn on the parent, because a parent parked waiting for its own
    children holds an admission slot while it waits -- and with a concurrency cap smaller
    than the number of children, that is a deadlock: the parent cannot finish until the
    children run, and the children cannot be admitted until the parent lets go. Splitting
    ENDS the parent; merging is a separate piece of work that starts when there is something
    to merge.
    """
    cid = campaign_id or campaign_id_for(parent_goal)
    return {
        "text": aggregation_prompt(parent_goal, records, limit_each=limit_each),
        "campaign_id": cid,
        "task_id": "%s-merge" % cid,
        "role": "aggregator",
        "parent_task_id": parent_task_id or cid,
        "depth": MAX_DEPTH,          # never splits again
        "priority": True,            # the campaign is finished; do not queue behind new work
    }


def aggregation_prompt(parent_goal, results, limit_each=1200):
    """What the parent is asked once its children are finished.

    The children's answers are given as material, and the parent is told which of them
    failed. Hiding the failures would produce a confident summary of an incomplete sweep --
    the exact defect the adversarial reviews kept finding in this work all day: a report that
    reads as complete because the gaps in it were never mentioned.
    """
    done = [r for r in results if (r.get("outcome") or "").upper() == "DONE"]
    missing = [r for r in results if (r.get("outcome") or "").upper() != "DONE"]

    parts = [parent_goal,
             "\n\n【分割実行の結果をまとめてください】",
             "この目標は %d 個のサブタスクに分割して並列実行しました。"
             "以下は各サブタスクの報告です。これらを統合して、最終的な回答を作成してください。"
             % len(results)]
    for r in results:
        head = "--- サブタスク %s / %s ---" % (r.get("subtask_index", "?"),
                                              (r.get("outcome") or "?"))
        body = (r.get("result") or "").strip()
        if len(body) > limit_each:
            body = body[:limit_each] + "\n…（以下略）"
        parts.append("%s\n%s" % (head, body or "(報告なし)"))

    if missing:
        parts.append(
            "\n【重要 — 未完了のサブタスクが %d 個あります】\n"
            "未完了: %s\n"
            "その範囲は取得できていません。取得できたかのように書かず、"
            "最終回答の中で「未取得」として明示してください。"
            % (len(missing), ", ".join(str(r.get("subtask_index", "?")) for r in missing)))
    else:
        parts.append("\n全サブタスクが完了しています。")

    parts.append("統合した最終回答を書き、最後の行に DONE と書いてください。"
                 "サブタスクの報告をそのまま並べるのではなく、"
                 "目標が求めている形式に統合してください。")
    return "\n".join(parts)


__all__ = ["SUBTASKS_READY", "SPLIT_JOB", "MAX_CHILDREN", "MIN_CHILDREN", "MAX_DEPTH",
           "fanout_ready", "subtasks_from", "child_goals", "aggregation_prompt",
           "campaign_id_for"]
