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

#: The agent's way of saying the goal should not be split at all.
#:
#: THE PROMPT USED TO DEMAND A SPLIT. It asked for 2〜12 subtasks and offered no other answer,
#: so an agent handed one indivisible investigation had to invent a division or stall -- and
#: both were observed. A judge with only one permitted verdict is not a judge.
#:
#: This is the live half of the splittability decision. relay/splittability.py is offline by
#: construction ("It makes NO live model call") and returns UNCERTAIN when its rules cannot
#: tell; `should_split` then read UNCERTAIN as "no". Now UNCERTAIN spends one turn asking the
#: agent, which can read the goal, and this is how it answers.
NO_SPLIT_MARKER = "NO_SPLIT"

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
    "最後の行に %s と書いてください。\n"
    "ただし、**分割すべきでないと判断したら分割しないでください。** 1つの調査を無理に割ると、"
    "どの断片も全体の文脈を失って answerable でなくなります。分割しない場合は、理由を1行書いて"
    "最後の行に %s とだけ書いてください（その場合はこの会話でそのまま実行してもらいます）。"
    % (MIN_CHILDREN, MAX_CHILDREN, SUBTASKS_READY, NO_SPLIT_MARKER)
)


def declined_split(resp) -> bool:
    """Did the agent answer that this goal should not be split?

    Checked BEFORE `fanout_ready`, because a reply may mention both markers -- the prompt
    names them together -- and a decline that is read as a ready split becomes an empty
    subtask list, which is handled as a MALFORMED split rather than as the answer it is.
    """
    up = (resp or "").upper()
    if NO_SPLIT_MARKER not in up:
        return False
    # `SUBTASKS_READY` does not contain `NO_SPLIT`, so there is no substring collision to
    # unpick; what matters is only which marker the agent ENDED on. Last line wins, and a
    # reply that names neither at the end falls back to "mentioned it at all".
    for line in reversed([l.strip() for l in (resp or "").splitlines() if l.strip()]):
        u = line.upper()
        if SUBTASKS_READY in u:
            return False
        if NO_SPLIT_MARKER in u:
            return True
    return True


#: The same request, asked after the work has started instead of before it.
#:
#: SEPARATE TEXT BECAUSE THE SITUATION IS DIFFERENT, not for variety. SPLIT_JOB opens with
#: 「実行はまだしないでください」, which is wrong for an agent that has been executing for six
#: turns, and it says nothing about what is already finished -- an agent told only "divide
#: this goal" re-divides the part it has already done, and the children redo it.
#:
#: It also has to be honest that declining is still allowed. The trigger is evidence, not
#: proof: a goal can run long for reasons a split does not fix, and an agent forced to split
#: one indivisible investigation produces the shape measured in campaign c7e01b58b1956, where
#: subtasks refused for want of the context the others held.
MIDRUN_SPLIT_JOB = (
    "【この作業を分割して並列実行に切り替えます】\n"
    "この会話は %d 回続けて『作業中』のまま完了に届いていません。1つの会話に収まらない"
    "分量である可能性が高いので、**残っている作業**を、互いに独立して実行できるサブタスクに"
    "分割してください。ここから先の実行はまだしないでください。\n"
    "重要:\n"
    "  1. **すでに完了した分は含めないこと。** 何がどこまで終わったかを1〜2行で先に書いてから、"
    "残りだけを分割してください（終わった分をもう一度やらせないため）\n"
    "  2. 各サブタスクは、この会話を見ていない別の会話が単独で実行できること"
    "（対象・期間・出力先を具体的に書く。「残りを続ける」は不可）\n"
    "  3. サブタスク同士で重複も抜けも無いこと\n"
    "%d〜%d 個に分割し、番号付きの箇条書きで列挙して、最後の行に %s と書いてください。\n"
    "分割しても解決しない性質の作業だと判断した場合は、理由を1行書いて最後の行に %s と"
    "だけ書いてください（その場合はこの会話でそのまま続行してもらいます）。"
)


def midrun_split_job(continues):
    """MIDRUN_SPLIT_JOB with the observed continue count filled in.

    The number is in the prompt because it is the EVIDENCE. "You have been going for six
    turns without finishing" is a fact the agent can weigh against what it knows about the
    remaining work; "please split this" is an instruction it can only obey.
    """
    return MIDRUN_SPLIT_JOB % (int(continues), MIN_CHILDREN, MAX_CHILDREN,
                               SUBTASKS_READY, NO_SPLIT_MARKER)


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
                cwd=None):
    """Turn the accepted steps into goal items the fleet can admit.

    NO `checks` PARAMETER, AND ITS REMOVAL IS THE POINT. It used to take the parent's
    acceptance checks and put the SAME object on every child -- measured 2026-09-13, three
    children of one pytest-gated goal all carried `{"type": "pytest", "args": "-q tests/"}`
    -- so each child's completion condition was a question about the whole goal while its
    own prompt forbade it to touch the other slices. A strict check then never passes until
    the siblings finish; a loose one passes for free the moment a sibling satisfies it, and
    `_salvage_via_checks` turns that into a salvaged DONE for a child that did nothing.

    The parameter is GONE rather than ignored: a caller that still has a whole-goal check
    must be made to say where it goes, and the answer is the merge (aggregation_goal takes
    `parent_checks`), not the children. Ignoring it silently would leave every existing
    caller believing its children are still verified.

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
    """Rebuild {campaign_id: {goal, n, cwd, checks, partial, merged, children}} from the ledger.

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
        if rec.get("kind") == "merged":
            # ALREADY ASSEMBLED. Written when the merge is queued, because `merged` used to
            # live only in memory -- so a run rebuilt from this file would queue the merge
            # again for every campaign it had ever finished, and the operator would get the
            # same combined answer a second time with no way to tell which was current.
            out.setdefault(cid, {"goal": "", "n": 0, "cwd": None, "checks": [],
                                 "partial": "", "children": []})["merged"] = True
            continue
        if rec.get("kind") == "campaign":
            out[cid] = {"goal": rec.get("goal") or "",
                        "n": int(rec.get("n") or 0),
                        "cwd": rec.get("cwd"),
                        # A "merged" line may arrive before OR after the header when two runs
                        # append concurrently, so it is carried across rather than reset.
                        "merged": bool(out.get(cid, {}).get("merged")),
                        # SAME REASON AS cwd. A run that dies after the split is rebuilt from
                        # this file, and a merge rebuilt without the parent's check is a merge
                        # nothing verifies -- silently, and only on the crash path.
                        "checks": rec.get("checks") or [],
                        "partial": rec.get("partial") or "",
                        "children": out.get(cid, {}).get("children", [])}
            continue
        entry = out.setdefault(cid, {"goal": "", "n": 0, "cwd": None, "checks": [],
                                     "partial": "", "merged": False, "children": []})
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

    CHECK DICTS, NOT SENTENCES, AND THAT IS THE WHOLE FIX. This returned bare strings, and
    `acceptance.normalize_checks` "silently drops non-dict members" -- so the list arrived at
    the worker as [], the worker took its `if not self.checks` branch ("no checks -> DONE
    accepted as before"), and the one gate standing between a merge and a confident report of
    an incomplete sweep never ran once. Measured 2026-09-13: aggregation_goal carried the
    string, goal_fields returned []. Four tests asserted the goal CARRIED it; none asked
    whether anything READ it.

    Three checks, because the recorded failure has three faces. The incident is two merges
    that ended DONE having written 「欠落なし」 with slices missing:

      * the gap numbers must appear -- what the old sentence asked for;
      * 「未取得」 must appear -- the word the merge prompt itself demands;
      * 「欠落なし」 must NOT appear -- the sentence actually observed, which no positive
        check can catch, since a reply can contain both.

    The number check is LENIENT by construction: a bare "2" also matches inside "2026", so it
    can pass on a coincidence. It cannot fail on one, which is the direction that matters --
    it never blocks a correct report, and the other two carry the strictness.
    """
    gaps = missing_slices(records)
    if not gaps:
        return []
    names = ", ".join(str(g) for g in gaps)
    return [
        {"type": "reply_contains", "all_of": [str(g) for g in gaps],
         "why": "未完了のサブタスク %s の番号に触れていない" % names},
        {"type": "reply_contains", "needle": "未取得",
         "why": "未完了があるのに『未取得』として明示していない"},
        {"type": "reply_contains", "needle": "欠落なし", "expect": False,
         "why": "未完了があるのに『欠落なし』と書いている"},
    ]


def aggregation_goal(parent_goal, records, *, campaign_id="", parent_task_id="",
                     limit_each=1200, cwd=None, parent_checks=None, parent_partial=""):
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
        "text": aggregation_prompt(parent_goal, records, limit_each=limit_each,
                                   parent_partial=parent_partial),
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
    # THE PARENT'S OWN CHECK LANDS HERE, NOT ON THE CHILDREN. It is a question about the
    # whole goal, and this is the worker for which that is the right question: the merge runs
    # in the parent's cwd and is the parent goal finishing. Copied onto each child instead
    # (which is what used to happen) it asked every slice about work it was told not to do.
    checks = [c for c in (parent_checks or []) if isinstance(c, dict)]
    checks.extend(merge_acceptance_checks(records))
    if checks:
        item["checks"] = checks
    return item


def aggregation_prompt(parent_goal, results, limit_each=1200, parent_partial=""):
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

    # A MID-RUN SPLIT HAS A PARENT THAT DID WORK, and ending it drops all of it -- the merge
    # reads child records only. Carried so the mechanism meant to rescue a long-running goal
    # does not destroy the part of it that was finished.
    #
    # ITS OWN BLOCK, NOT A RECORD, AND NOT MARKED DONE. It was briefly recorded as
    # `{"subtask_index": 0, "outcome": "DONE"}` -- chosen so it would not move the gap check,
    # which is choosing a convenient falsehood in the one mechanism built to stop a report
    # reading complete because its gaps were never named. The parent did NOT finish; that is
    # why it was split. So it is material, labelled unverified, and it is not a slice: nothing
    # can count it as one, and `missing_slices` never sees it.
    if parent_partial:
        parts.append(
            "\n【分割前に、この目標の会話が終えていた分（未検証・途中経過）】\n"
            "この会話は完了に至らず分割されました。以下はその時点までの報告で、"
            "完了の証明ではありません。内容が下のサブタスク報告と重複する場合は"
            "サブタスク側を採用し、食い違う場合はその旨を明記してください。\n"
            + (parent_partial[:limit_each] if len(parent_partial) > limit_each
               else parent_partial))
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


# ── FAN-OUT FAMILY VIEW (derived, for the cockpit)  ───────────────────────────────────
# THE LINEAGE WAS ALREADY IN status.json AND NOTHING READ IT. Every worker entry the
# runner writes already carries campaign_id / parent_task_id / role / depth / subtask_index
# (relay/fleet_runner.py _snapshot + _final_worker_entry). But raw ids are not a display:
# a person looking at the cockpit cannot tell a parent that split from a child slice, an
# aggregator waiting for its family from one already merging, or -- the failure this was
# written for -- a goal that PROPOSED a split (emitted SUBTASKS_READY) whose children were
# never admitted, which looks identical to an ordinary single-goal worker.
#
# This is a PURE projection of the snapshot the runner already produces. It invents no ids,
# opens no files, and does not touch when fan-out fires -- it only reads the workers list and
# labels each entry so the UI can render the family without inferring anything itself.

_TERMINAL_OK = {"DONE", "FANOUT"}


def _wnorm(w):
    """Read a worker snapshot dict tolerantly (missing keys -> neutral defaults)."""
    g = w.get
    return {
        "name": g("name") or "",
        "campaign_id": g("campaign_id") or "",
        "task_id": g("task_id") or "",
        "parent_task_id": g("parent_task_id"),
        "role": (g("role") or "").lower(),
        "depth": int(g("depth") or 0),
        "subtask_index": g("subtask_index"),
        "outcome": (g("outcome") or "").upper(),
        "status": (g("status") or "").lower(),
        "last": g("last") or g("display_result") or g("last_response") or "",
    }


def fanout_family_view(workers):
    """Label each worker with a display-ready fan-out marker derived from lineage already
    present in the snapshot. Returns {worker_name: marker_dict}.

    marker_dict keys (always present):
      kind            : "solo" | "parent" | "child" | "aggregator" | "stalled_parent"
      campaign_id     : the family id ("" for a solo worker)
      label           : short English one-liner for the card badge
    kind-specific keys:
      child           -> subtask_index, subtask_of (parent name or "")
      parent/stalled  -> children_total, children_done, missing_slices, fanin_state,
                         split_proposed_not_run (True only for stalled_parent)
      aggregator      -> children_total, children_done, missing_slices, fanin_state

    fanin_state (parent/aggregator): "pending" (children still running),
      "ready" (all children finished, no merge yet), "merging" (an aggregator is running),
      "merged" (an aggregator finished ok).

    Nothing here fabricates: a solo worker that never proposed a split is honestly "solo";
    only a worker that emitted SUBTASKS_READY yet has no admitted children is flagged
    "stalled_parent" -- the split that was proposed and silently never ran.
    """
    ws = [_wnorm(w) for w in (workers or [])]

    kids = {}          # campaign_id -> [child records]
    aggs = {}          # campaign_id -> [aggregator records]
    by_task = {}       # task_id -> record (to name a child's parent)
    for w in ws:
        if w["task_id"]:
            by_task[w["task_id"]] = w
        if w["campaign_id"]:
            if w["role"] == "subtask":
                kids.setdefault(w["campaign_id"], []).append(w)
            elif w["role"] == "aggregator":
                aggs.setdefault(w["campaign_id"], []).append(w)

    def _child_records(cid):
        recs = []
        for c in kids.get(cid, []):
            recs.append({
                "subtask_index": c["subtask_index"],
                "outcome": c["outcome"],
                "finished": c["outcome"] in _TERMINAL_OK,
            })
        return recs

    def _fanin(cid):
        recs = _child_records(cid)
        total = len(recs)
        done = sum(1 for r in recs if r["finished"])
        miss = missing_slices(collapse_retries(recs)) if recs else []
        agg_list = aggs.get(cid, [])
        agg_ok = any(a["outcome"] in _TERMINAL_OK for a in agg_list)
        agg_running = any(a["outcome"] not in _TERMINAL_OK for a in agg_list)
        if agg_ok:
            state = "merged"
        elif agg_running:
            state = "merging"
        elif recs and ready_to_aggregate(recs):
            state = "ready"
        else:
            state = "pending"
        return total, done, miss, state

    view = {}
    for w in ws:
        cid = w["campaign_id"]
        name = w["name"]
        if w["role"] == "subtask":
            parent = by_task.get(w["parent_task_id"] or "")
            idx = w["subtask_index"]
            view[name] = {
                "kind": "child",
                "campaign_id": cid,
                "subtask_index": idx,
                "subtask_of": parent["name"] if parent else "",
                "label": ("subtask %s" % idx) if idx is not None else "subtask",
            }
            continue
        if w["role"] == "aggregator":
            total, done, miss, state = _fanin(cid)
            view[name] = {
                "kind": "aggregator",
                "campaign_id": cid,
                "children_total": total,
                "children_done": done,
                "missing_slices": miss,
                "fanin_state": state,
                "label": "merge %d/%d" % (done, total),
            }
            continue
        has_kids = bool(kids.get(cid)) if cid else False
        if has_kids:
            total, done, miss, state = _fanin(cid)
            view[name] = {
                "kind": "parent",
                "campaign_id": cid,
                "children_total": total,
                "children_done": done,
                "missing_slices": miss,
                "fanin_state": state,
                "split_proposed_not_run": False,
                "label": "split %d/%d" % (done, total),
            }
            continue
        if fanout_ready(w["last"]):
            view[name] = {
                "kind": "stalled_parent",
                "campaign_id": cid,
                "children_total": 0,
                "children_done": 0,
                "missing_slices": [],
                "fanin_state": "pending",
                "split_proposed_not_run": True,
                "label": "split proposed, no children ran",
            }
            continue
        view[name] = {"kind": "solo", "campaign_id": cid, "label": ""}
    return view


__all__ = ["SUBTASKS_READY", "SPLIT_JOB", "MAX_CHILDREN", "MIN_CHILDREN", "MAX_DEPTH",
           "fanout_ready", "subtasks_from", "child_goals", "aggregation_prompt",
           "campaign_id_for",
    "NO_SPLIT_MARKER", "declined_split", "MIDRUN_SPLIT_JOB", "midrun_split_job",
    "missing_slices", "merge_acceptance_checks", "campaigns_from_ledger",
    "collapse_retries", "ready_to_aggregate", "aggregation_goal",
    "fanout_family_view",
]
