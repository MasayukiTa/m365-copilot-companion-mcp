# -*- coding: utf-8 -*-
"""Did the worker follow the procedure the matched skill prescribes? A generic, deterministic check.

WHY (2026-10-15, Documents\\フリート設計見直し_20261014\\DB未使用の原因.md). Two fleet goals were
answered from a spreadsheet and from "nothing found" while the skill library held a procedure
that prescribes a different source. Nothing between the model's DONE and the report compared
what the worker DID with what the matched skill SAID to do; the reviewer only checked the answer
against itself.

THIS MODULE IS DOMAIN-FREE BY RULE. It contains no tool name, no system name and no topic word.
A skill may declare, in its own frontmatter, ``requires_tools: [<tool or glob>, ...]`` (see
relay/skills.py::_validate_requires_tools); the names appear ONLY there. This module reads that
list, reads the tool ledger, and answers one question: was at least one listed tool called by
this worker since it started? tests/test_no_domain_words_in_harness.py keeps the vocabulary out.

ATTRIBUTION IS WHAT THE LEDGER ALLOWS, AND THE VERDICT SAYS SO. Ledger rows carry the worker
only when exactly one worker was in flight (tools/tool_ledger._window_identity); in a fleet they
carry an empty worker and an MCP session hash. So:

  * "followed"       a row of THIS worker calls a listed tool.
  * "not_followed"   this worker has rows and none is listed; or NO row anywhere in the worker's
                     window calls a listed tool (nobody did, so this worker did not).
  * "unverifiable"   no row of this worker's own, and some other row in the window DID call a
                     listed tool: it may have been this worker's. Never blocks (a check that
                     cannot tell must not steer a worker that may have done the work).
  * "n/a"            nothing to check (no requirement declared, or the ledger is unreadable).
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

FOLLOWED = "followed"
NOT_FOLLOWED = "not_followed"
UNVERIFIABLE = "unverifiable"
NOT_APPLICABLE = "n/a"

#: Steers a worker may receive before the harness gives up and settles STUCK.
MAX_STEERS = 2

#: The cause named in the stuck reason (relay/stuck_cause.py kind "procedure").
STUCK_CAUSE_TEXT = "skill procedure not followed"

#: The sentence every matched-skill frame carries. Generic by construction: it says what to do
#: with WHATEVER the skill prescribes, and it is the only place the harness states the rule.
FRAME_RULE = (
    "この手順が指定する情報源・ツール・順序を使うこと。手順が指定するものの代わりに別の情報源"
    "（表計算・ファイル・検索など）を使って答えない。手順を最後まで実行できない場合は、"
    "どこで何が理由でできないかを具体的に書く。回答の1行目に、実際に使った情報源を書く。"
)

#: Sent when the worker claims DONE but its ledger shows none of the tools the matched
#: skill declares as required. %s = the skill name, %s = the declared tools.
STEER_TEXT = (
    "承認済み手順 `%s` に従う作業です。この手順は次のツールの使用を前提としていますが、"
    "あなたがそれらを呼んだ記録がありません: %s\n"
    "手順どおりに今それを実行して答え直してください。実行できない場合は、何が理由でできない"
    "のかを具体的に書き、最後の行を STUCK: <理由> にしてください。別の情報源での回答は"
    "受け付けません。回答の1行目に使った情報源を書くこと。"
)


@dataclass
class Verdict:
    state: str
    skill: str = ""
    required: list = field(default_factory=list)
    called: list = field(default_factory=list)      # listed tools seen in the window
    attributed: bool = False
    rows_considered: int = 0
    detail: str = ""

    @property
    def blocks(self) -> bool:
        return self.state == NOT_FOLLOWED


def steer_text(skill: str, required) -> str:
    return STEER_TEXT % (skill, ", ".join(required))


def tool_listed(tool: str, required) -> bool:
    """Is `tool` one of the declared names, or matched by a declared glob?"""
    t = str(tool or "")
    for pat in required or ():
        if t == pat or (re.search(r"[*?]", pat) and fnmatch.fnmatchcase(t, pat)):
            return True
    return False


def _calls(rows, since: float, until: float):
    for r in rows or ():
        if r.get("event") != "call":
            continue
        ts = float(r.get("ts") or 0.0)
        if ts < since or (until and ts > until):
            continue
        yield r


def check(rows, *, skill: str, required, worker: str, since: float, until: float = 0.0) -> Verdict:
    """Judge one worker against one skill's declared tools. Pure; never raises.

    `rows` are parsed tool-ledger rows (stuck_cause.read_tail()). An empty `rows` list means the
    ledger could not be read (or is empty) and yields n/a: not asking is not the same as
    having been answered.
    """
    required = [str(x) for x in (required or []) if str(x).strip()]
    v = Verdict(state=NOT_APPLICABLE, skill=skill, required=required)
    try:
        if not required or not rows:
            return v
        own = [r for r in _calls(rows, since, until) if str(r.get("worker") or "") == worker]
        anyone = [r for r in _calls(rows, since, until)]
        v.rows_considered = len(anyone)
        own_hits = sorted({r.get("tool") for r in own if tool_listed(r.get("tool"), required)})
        if own_hits:
            v.state, v.called, v.attributed = FOLLOWED, own_hits, True
            return v
        if own:
            v.state, v.attributed = NOT_FOLLOWED, True
            v.detail = "%d of this worker's own tool calls, none of them listed" % len(own)
            return v
        hits = sorted({r.get("tool") for r in anyone if tool_listed(r.get("tool"), required)})
        if hits:
            v.state, v.called = UNVERIFIABLE, hits
            v.detail = ("the ledger does not attribute calls to this worker; other conversations "
                        "called %s in the window" % ", ".join(hits))
            return v
        v.state = NOT_FOLLOWED
        v.detail = "no call of any listed tool by any conversation since the worker started"
        return v
    except Exception:
        return Verdict(state=NOT_APPLICABLE, skill=skill, required=required)
