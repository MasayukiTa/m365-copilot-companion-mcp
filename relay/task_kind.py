# -*- coding: utf-8 -*-
"""A derived label for WHAT KIND OF WORK a goal asks for (roadmap slice D0).

WHY THIS EXISTS. Later slices want to judge the fleet per kind of work (a role that is good at
bulk record checks may be poor at code changes). Nothing recorded says what kind a run was:
  * relay/task_router.py DESTINATION types (shell, python, coding, research, fleet_goal, ...)
    classify a queued JOB by where it runs. Every job file the fleet ever finished is the single
    type `fleet_goal` (1587 of 1587 measured 2026-10-20), so the type tells nothing apart.
  * relay/splittability.py judges a goal SPLIT / NO_SPLIT / UNCERTAIN. That is a property of the
    goal's shape, it is not stored per run, and it is not a kind of work.
  * relay/fanout.py's family view labels solo / parent / child / aggregator / stalled_parent.
    That is the position in a fan-out family, which the capability profile already records as
    the ROLE.
So the label is derived here from the goal text, by fixed rules, and only by the report
scripts: no fleet module imports this file and no record is ever written with it.

CONTRACT
  * Deterministic: the same text gives the same label (no clock, no randomness, no state).
  * The first rule that matches wins, in the order of RULES. A goal that matches none is
    "unknown". A label is never guessed from weak hints: a rule needs one of its own markers.
  * Non-text input (None, numbers) is "unknown".
The markers were read off the 458 goals of the live history (2026-10-20). They describe wording
the operator and the agents actually used. A new kind of work stays "unknown" until a rule is
added on purpose, which keeps a per-kind table from filling with wrong guesses.
"""
from __future__ import annotations

UNKNOWN = "unknown"
CODE_CHANGE = "code_change"
BULK_CHECK = "bulk_check"
DATA_QUERY = "data_query"
M365_LOOKUP = "m365_lookup"
TOOL_PROBE = "tool_probe"
LOCAL_FILES = "local_files"
WRITING = "writing"     # prose or research write-ups produced from the model's own knowledge

#: (label, markers) in priority order. Markers are matched against the casefolded goal text.
#: Order matters: a code task that mentions a database, or a database task that names a repo
#: path, must land on its own kind, so the narrower kinds come first.
RULES = (
    (CODE_CHANGE, ("implementation task", "small fix", "doc rewrite", "implement the task")),
    (BULK_CHECK, ("突合", "フリガナ", "台帳")),
    (DATA_QUERY, ("mcl db", "mcl-db", "adams", "odbc", "sql server", "catalog_search",
                  "社内db", "不良", "ロット", "ピール", "引きはがし", "検査件数", "検査データ",
                  "ワニス", "銅箔", "ガラスクロス", "設備", "号機")),
    (M365_LOOKUP, ("メール", "予定を", "teams", "onedrive", "sharepoint", "上司", "outlook",
                   "受信")),
    (TOOL_PROBE, ("__noop_probe__", "harmless", "run_python")),
    (WRITING, ("handbook", "booklet", "manual", "briefing", "deliverable", "primer", "chapter",
               "glossar", "field guide", "study pack", "study sheet", "explainer", "recipe book",
               "two workers", "further worker", "in two sentences", "two parts", "two levels",
               "冊子", "成果物", "レポート", "サブタスク", "互いに独立して", "2分野", "3項目",
               "二つの部", "二段階")),
    (LOCAL_FILES, ("c:\\", "relay/", "relay\\", "tools/", "tools\\", ".py", "git repository",
                   "read-only", "読むだけ", "を読んで", "リポジトリ")),
)

KINDS = tuple(label for label, _ in RULES) + (UNKNOWN,)


def task_kind(goal):
    """The kind of work `goal` asks for: one of KINDS. "unknown" when no rule's marker is in it."""
    if not isinstance(goal, str) or not goal.strip():
        return UNKNOWN
    text = goal.casefold()
    for label, markers in RULES:
        for marker in markers:
            if marker in text:
                return label
    return UNKNOWN
