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

import json

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


def collapse_retries(records):
    """One record per slice of the split, keeping the attempt that actually worked.

    A subtask that fails transiently is re-queued, so a family can end up holding two
    records for the same slice: the attempt that went STUCK and the retry that finished it.
    Reporting both would tell the merge that range both failed and succeeded, and the merge
    is required to name failures -- so it would mark a range 未取得 that is sitting in front
    of it, completed, in the very next record.

    A DONE beats anything else for the same slice. Records with no slice number are left
    alone: there is nothing to collapse them against.

    A CANDIDATE IS NOT A RETRY, and the key says so. best-of-N runs the SAME goal text N times
    on purpose and keeps every answer for a selector to choose between -- but N such workers
    carry the same slice number, so this function saw a family that had been retried N-1 times
    and collapsed it to one. Measured before the fix: three DONE records on one slice went in
    and one came out, the first. The candidates never reached the selector, which is the whole
    mechanism, and nothing in the output said any had been dropped.
    The rule is not wrong; the two relationships are simply different. A retry REPLACES the
    attempt before it and a candidate SITS BESIDE it, so they cannot share a key. Adding
    `candidate_index` to the key keeps retries of one candidate collapsing exactly as before
    -- absent means None, which is what every existing record has -- while different
    candidates never collapse into each other.
    """
    best: dict[Any, dict] = {}
    loose = []
    for rec in records:
        idx = rec.get("subtask_index")
        if idx is None:
            loose.append(rec)
            continue
        key = (idx, rec.get("candidate_index"))
        current = best.get(key)
        if current is None:
            best[key] = rec
            continue
        if (current.get("outcome") or "").upper() != "DONE" and \
                (rec.get("outcome") or "").upper() == "DONE":
            best[key] = rec
    # Ordered by slice, then by candidate, so a family reads in a stable order whether or not
    # candidates are in play. `or 0` because the ordinary record has no candidate number.
    return sorted(best.values(),
                  key=lambda r: (r.get("subtask_index"),
                                 r.get("candidate_index") or 0)) + loose


def ready_to_aggregate(records):
    """Have all of a campaign's sub-tasks finished?

    `records` are that campaign's children: {"finished": bool, ...}. Empty means no children
    were ever admitted, which is not "ready" -- aggregating nothing would produce a confident
    summary of work that never ran.
    """
    return bool(records) and all(r.get("finished") for r in records)


def campaigns_from_ledger(lines):
    """Rebuild {campaign_id: {goal, n, cwd}} from the campaigns ledger.

    THE LEDGER HAD NO READER. relay_fleet wrote one line per child so that a run dying
    mid-split would leave a trace of work already queued -- and nothing anywhere opened the
    file. Measured 2026-08-28: one writer, zero readers, in the whole repository.

    That matters most in the case the file was written for. On FleetContextLost the fleet
    re-enters run_relay_fleet with a fresh process, so the in-memory `campaigns` dict is
    empty and `_unfinished()` returns only goals -- never families. A campaign split before
    the crash is never merged again: its children may all finish, and the answer they were
    collected for is never assembled.

    The header lines this reads did not exist either; the child lines carry a campaign id
    and a slice number but not the parent goal, which is the one thing a merge needs. So
    both halves were missing, and one without the other is still unreadable.

    Tolerant by construction: a truncated final line (the run died mid-write, which is the
    scenario) must not lose the families above it.
    """
    out = {}
    for line in lines or []:
        line = (line or "").strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue                 # a half-written line, most likely the last one
        cid = rec.get("campaign_id")
        if not cid:
            continue
        if rec.get("kind") == "campaign":
            out[cid] = {"goal": rec.get("goal") or "",
                        "n": int(rec.get("n") or 0),
                        "cwd": rec.get("cwd"),
                        "children": out.get(cid, {}).get("children", [])}
            continue
        entry = out.setdefault(cid, {"goal": "", "n": 0, "cwd": None, "children": []})
        entry["children"].append(rec)
    # A FAMILY WITHOUT ITS HEADER CANNOT BE MERGED, and saying so is better than returning
    # a campaign whose parent goal is the empty string -- which would merge into nothing.
    return {cid: fam for cid, fam in out.items() if fam.get("goal")}


def missing_slices(records):
    """The subtask numbers that did not finish DONE. [] when the sweep was complete.

    The merge prompt already asks for these to be named. Nothing checked that they were.
    Measured over the runs on record: two campaigns reached the merge with gaps (8/9 and
    4/8), and in the one whose transcripts survive, both merges that finished DONE wrote
    that nothing was missing. The prompt asked; the answer did not comply; no one looked.

    This is the counterpart of subtasks_from, which refuses a split proposal it cannot
    parse. The split has had that check since it was written. The merge has not.
    """
    out = []
    for rec in records or []:
        if (rec.get("outcome") or "").upper() == "DONE":
            continue
        idx = rec.get("subtask_index")
        if idx is not None:
            out.append(idx)
    return sorted(out)


def merge_acceptance_checks(records):
    """Acceptance checks for the merge goal: every unfinished slice must be named.

    A whole-answer check, not a per-slice one, because the merge is asked for an account
    and the account has to mention the gaps by number. When the sweep was complete there
    is nothing to check -- an empty list, not a check that passes trivially, so a reader
    can tell the difference between 'checked and clean' and 'nothing to check'.
    """
    gaps = missing_slices(records)
    if not gaps:
        return []
    return ["未取得または未完了のサブタスク %s について、回答本文でその番号に触れていること"
            % ", ".join(str(g) for g in gaps)]


def aggregation_goal(parent_goal, records, *, campaign_id="", parent_task_id="",
                     limit_each=1200, cwd=None):
    """The goal item that merges a finished campaign.

    A goal rather than a turn on the parent, because a parent parked waiting for its own
    children holds an admission slot while it waits -- and with a concurrency cap smaller
    than the number of children, that is a deadlock: the parent cannot finish until the
    children run, and the children cannot be admitted until the parent lets go. Splitting
    ENDS the parent; merging is a separate piece of work that starts when there is something
    to merge.
    """
    cid = campaign_id or campaign_id_for(parent_goal)
    item = {
        "text": aggregation_prompt(parent_goal, records, limit_each=limit_each),
        "campaign_id": cid,
        "task_id": "%s-merge" % cid,
        "role": "aggregator",
        "parent_task_id": parent_task_id or cid,
        "depth": MAX_DEPTH,          # never splits again
        "priority": True,            # the campaign is finished; do not queue behind new work
    }
    # THE SAME WORKING DIRECTORY THE CHILDREN HAD. child_goals passes cwd down; this did
    # not, and the merge is asked to write a combined file and report its path -- from
    # whatever directory it happened to start in.
    if cwd:
        item["cwd"] = cwd
    checks = merge_acceptance_checks(records)
    if checks:
        item["checks"] = checks
    return item


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

    # THE MERGE MUST NOT INHERIT THE DISEASE IT CURES. Asked for "the final answer", an
    # agent holding eight reports of several hundred rows each tries to re-emit all of them
    # in one response -- which is the size problem fan-out exists to avoid, arriving at the
    # last step. The first live merge ran fourteen turns and went STUCK without producing
    # anything. So what is asked for here is bounded by construction: an account of what was
    # collected and where it is, with the rows themselves only inlined when they are few.
    parts.append(
        "\n【統合のしかた — 分量に注意】\n"
        "サブタスクの報告をそのまま並べ直さないでください。また、"
        "全件を1つの応答に書き出そうとしないでください（それができない分量だから分割しています）。\n"
        "次を書いてください:\n"
        "  1. 担当範囲ごとの取得件数と、その範囲が完了したか（根拠となる終端確認も）\n"
        "  2. 取得できなかった範囲を「未取得」として明示（無ければ「欠落なし」）\n"
        "  3. 各サブタスクが成果物をファイルに保存している場合は、そのパスを一覧する\n"
        "  4. 目標が明示的に求めている要点（特に必須項目として名指しされたもの）への回答\n"
        "全件の表が必要で、かつ1応答に収まらない場合は、"
        "1つのファイルに統合して保存し、そのパスと総件数を報告してください。\n"
        "最後の行に DONE と書いてください。")
    return "\n".join(parts)


__all__ = ["SUBTASKS_READY", "SPLIT_JOB", "MAX_CHILDREN", "MIN_CHILDREN", "MAX_DEPTH",
           "fanout_ready", "subtasks_from", "child_goals", "aggregation_prompt",
           "campaign_id_for",
    "missing_slices", "merge_acceptance_checks", "campaigns_from_ledger",
    "collapse_retries", "ready_to_aggregate", "aggregation_goal",
]
