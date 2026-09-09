# -*- coding: utf-8 -*-
"""Rule-based fan-out splittability judge (codex-plan item 6).

Prior state: the only fan-out trigger anywhere in the codebase was
`len(text) >= AUTOSTART_FANOUT_MIN_CHARS` (task_router.py, default 600
chars) -- a pure length proxy with no relationship to whether the goal's
work is actually independent/parallelizable. The plan's own measurement:
28.4% of a 387-goal sample crossed the threshold, median 367 chars, and a
real 67-char job did NOT cross it despite (per the plan) possibly being
splittable.

This module replaces the *judgment*, not the *trigger point*: it decides
WHETHER a goal is splittable and records WHY, based on independence and
parallelizability signals actually observed in this repo's own real fleet
transcripts (.fleet/**/transcripts/*.jsonl(.gz), 2026-09-09 extraction,
1214 real goal-carrying transcripts after excluding SWE-bench harness
prompts, 562 unique texts). It makes NO live model call -- pure, offline,
unit-testable -- mirroring tools/command_judge.py's own shape: a closed
decision enum, the module records its OWN reason, and genuine uncertainty
is never silently treated as permission to split (the same "failure is not
permission" principle command_judge.py states for JudgeUnavailable, applied
here to UNCERTAIN).

Real signals this ruleset is built from (not guessed):

  * Some real goals are ALREADY fan-out children -- they carry their own
    "全体の N/M" range marker and an explicit "他の範囲は別の会話が並行して
    担当しているので、手を出さないこと" instruction. These are leaves, not
    candidates for further splitting; misjudging them would recurse.

  * Some real goals restate the ENTIRE previous goal for context and then
    append one new instruction after "追加指示:" (a "前回タスクの続き"
    continuation). Total character count is dominated by restated context,
    not new work -- this is the length-proxy failure mode the plan names,
    observed directly rather than inferred.

  * The clearest genuinely-independent real example: "以下の劇場について
    1館ずつWeb検索で調べてください" over a bracketed list of a dozen named
    theaters -- each target needs its own search, no shared state, no
    ordering dependency. Structurally identical shape recurs for file
    lists, repo lists, per-branch lookups.

  * The clearest genuinely-NOT-independent real example: a numbered
    (1)(2)(3)... list of sub-questions that are all facets of ONE case/
    email-thread investigation, sharing the same underlying source
    documents. This shape was fanned out in real production (campaign
    c7e01b58b1956, 2026-09-09, verified during item 5's investigation) and
    left 4 of 7 subtasks REFUSED -- splitting a single shared-context
    investigation into independent workers starved each child of context
    the others held. This ruleset treats that shape as UNCERTAIN, not
    SPLIT, specifically because it has already been measured to fail.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict

SPLIT = "SPLIT"
NO_SPLIT = "NO_SPLIT"
UNCERTAIN = "UNCERTAIN"  # Not enough signal either way. A caller must NOT
                          # treat this as permission to fan out -- same rule
                          # command_judge.py states for JudgeUnavailable:
                          # failure/uncertainty is not permission.

_DECISIONS = (SPLIT, NO_SPLIT, UNCERTAIN)

_CHILD_MARKERS = (
    re.compile(r"全体の\s*\d+\s*/\s*\d+"),
    re.compile(r"他の範囲は別の(会話|ワーカー|worker)が並行して"),
    re.compile(r"担当範囲を完了したら"),
)

_EXPLICIT_SPLIT_HINT = (
    re.compile(r"日付を[^。]{0,20}(区切|分けて)"),
    re.compile(r"この作業を分割する場合"),
    re.compile(r"分割し(て|た)場合"),
    re.compile(r"全体を\s*\d+\s*(件|日|週|回)ずつ"),
)

_PER_ITEM_LOOKUP_HINT = re.compile(
    r"(1\s*(館|件|校|店|人|社|拠点|品種)\s*ずつ|それぞれ(の|)\s*1\s*(館|件|校|店)\s*ずつ)"
    r"[^。]{0,40}(調べ|検索|確認)"
)

# A multi-MONTH range ("1〜3月", "2026年1月〜3月", "1月から4月") is this repo's own established
# fan-out convention, not a guess: relay_fleet.py's SPLIT_JOB composition comment (and the
# mail-lookup Skill it quotes) documents slicing exactly this shape ("a quarter becomes
# months", "a month becomes 上旬/中旬/下旬"). Two connector shapes both appear in real goals --
# compact ("1〜3月", no 月 on the first number) and spelled out ("1月から4月") -- so both are
# matched. Deliberately month-only, not day-level: a first draft also matched "日" and caught
# a false positive on "4月28-30日に測定した" -- an incidental 3-day span naming when a single
# past event happened, buried inside one numbered sub-question of an otherwise single-case
# investigation, not a task-scoping range implying the WHOLE goal chops by period.
_DATE_RANGE_SPAN = re.compile(
    r"\d+\s*月?\s*[〜～\-–]\s*\d+\s*月|\d+\s*月\s*から\s*\d+\s*月"
)

_NUMBERED_ITEM = re.compile(r"[\(（]\s*\d+\s*[\)）]")

_CONTINUATION_MARKER = re.compile(r"前回タスクの続き")
_ADDITIONAL_INSTRUCTION = re.compile(r"追加指示[:：]\s*(.+)", re.S)

_SHORT_NO_SPLIT_LEN = 200


@dataclass
class Verdict:
    decision: str
    reason: str
    signals: Dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.decision not in _DECISIONS:
            raise ValueError("unknown decision %r; known: %r" % (self.decision, _DECISIONS))

    @property
    def should_split(self) -> bool:
        """True only for an affirmative SPLIT verdict. UNCERTAIN and
        NO_SPLIT both resolve to "don't fan out" -- callers must use this,
        not `decision == SPLIT`, so an UNCERTAIN result can never be
        silently mistreated as permission by a future edit that forgets
        the three-way enum."""
        return self.decision == SPLIT


def _unwrap_continuation(text: str) -> str:
    """A "前回タスクの続き" goal restates the ENTIRE previous goal for
    context, then appends one new instruction after "追加指示:". Judge
    splittability on the new instruction alone when this shape is present;
    the restated context is not new work and must not inflate the length
    signal. Falls back to the full text if the marker is absent or the
    expected "追加指示:" tail is missing."""
    if not _CONTINUATION_MARKER.search(text):
        return text
    m = _ADDITIONAL_INSTRUCTION.search(text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return text


def _count_bracketed_list_items(text: str) -> int:
    """Largest comma-separated item count found inside any full-width
    bracket 【...】 span. Used to detect a flat catalogue of independent
    named targets (theaters, filenames, repos) as opposed to prose."""
    best = 0
    for m in re.finditer(r"【([^】]{4,400})】", text):
        inner = m.group(1)
        # "、,，" for prose-style lists (the theater examples); "/" for the
        # key=value catalogue shape seen in the real furigana-batch goal
        # ("氏名=ローマ字 / 氏名=ローマ字 / ...", one entry per employee).
        items = [p.strip() for p in re.split(r"[、,，/]", inner) if p.strip()]
        if len(items) >= 3:
            best = max(best, len(items))
    return best


def judge(text: str) -> Verdict:
    """Judge whether `text` (a goal) describes independent, parallelizable
    work. Pure function: no I/O, no model call, safe to call for every
    goal on every admission path."""
    if not text or not text.strip():
        return Verdict(NO_SPLIT, "empty goal text cannot be split", {})

    for pat in _CHILD_MARKERS:
        if pat.search(text):
            return Verdict(
                NO_SPLIT,
                "already a fan-out child (carries its own N/M range marker "
                "and a 'do not touch other ranges' instruction); not a "
                "candidate for further splitting",
                {"is_child": True},
            )

    effective = _unwrap_continuation(text)
    unwrapped = effective is not text

    for pat in _EXPLICIT_SPLIT_HINT:
        if pat.search(effective):
            return Verdict(
                SPLIT,
                "goal text itself instructs chunking (author-specified "
                "split hint) -- the strongest available signal",
                {"unwrapped_continuation": unwrapped},
            )

    if _PER_ITEM_LOOKUP_HINT.search(effective):
        n = _count_bracketed_list_items(effective)
        return Verdict(
            SPLIT,
            "explicit per-item lookup phrasing ('1館ずつ調べて' etc.) over "
            "an enumerated list of independent named targets (%d found in "
            "a bracketed catalogue); each target needs its own search with "
            "no shared dependency on the others" % n,
            {"bracketed_list_items": n, "unwrapped_continuation": unwrapped},
        )

    _date_range_m = _DATE_RANGE_SPAN.search(effective)
    _first_numbered_m = _NUMBERED_ITEM.search(effective)
    if _date_range_m and not (_first_numbered_m and
                              _date_range_m.start() > _first_numbered_m.start()):
        # A range mentioned AFTER the first numbered sub-item (e.g. "(6)1月〜7月の間に
        # 実施した調査" as one question among several about a single email) is evidence INSIDE
        # a single-case investigation, not the task's own scope statement -- do not let it
        # short-circuit past the numbered-item check below, which is what correctly recognises
        # that shape. Real false positive this guards (2026-09-09): a goal reporting on ONE
        # email's content and relationships had item (6) ask about "1月〜7月の間の調査", and
        # without this ordering check the whole single-case investigation was misjudged SPLIT.
        return Verdict(
            SPLIT,
            "spans a multi-period date range (multiple months/days/weeks) "
            "stated as the goal's own scope, before any numbered sub-item; "
            "this repo's own mail-lookup skill convention slices exactly "
            "this shape (a quarter into months, a month into 旬), and the "
            "real corpus's own large multi-month mail search needed this "
            "same chunking in practice",
            {"unwrapped_continuation": unwrapped},
        )

    n_bracket_items = _count_bracketed_list_items(effective)
    if n_bracket_items >= 4:
        return Verdict(
            SPLIT,
            "bracketed catalogue of %d comma-separated items with no shared "
            "narrative context; independently investigable" % n_bracket_items,
            {"bracketed_list_items": n_bracket_items,
             "unwrapped_continuation": unwrapped},
        )

    numbered = len(_NUMBERED_ITEM.findall(effective))
    if numbered >= 3:
        # Numbered sub-questions about ONE case/thread share the same
        # underlying source documents and search context. Measured in real
        # production (campaign c7e01b58b1956, verified 2026-09-09 during
        # item 5's investigation): naively fanning this exact shape out 7
        # ways left 4/7 subtasks REFUSED. Record the uncertainty rather
        # than guessing either way -- do not recommend a split here.
        return Verdict(
            UNCERTAIN,
            "%d numbered sub-items found, but they read as facets of one "
            "case/thread rather than independent targets (no bracketed "
            "catalogue, no per-item lookup phrasing) -- this exact shape "
            "over-split in real production (campaign c7e01b58b1956, 4/7 "
            "subtasks refused); default is NOT to split on uncertainty" % numbered,
            {"numbered_items": numbered, "unwrapped_continuation": unwrapped},
        )

    if len(effective) < _SHORT_NO_SPLIT_LEN:
        return Verdict(
            NO_SPLIT,
            "short with no independence/enumeration signal; reads as a "
            "single lookup or computation",
            {"len": len(effective), "unwrapped_continuation": unwrapped},
        )

    return Verdict(
        UNCERTAIN,
        "long text (%d chars) but no independence signal found (no "
        "per-item lookup phrasing, no bracketed catalogue of >=4 items, no "
        "explicit split hint, fewer than 3 numbered sub-items); length "
        "alone is not evidence of splittability" % len(effective),
        {"len": len(effective), "unwrapped_continuation": unwrapped},
    )
