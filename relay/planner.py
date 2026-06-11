"""planner.py -- plan-first execution (Claude-Code style): propose a plan, let the user
approve or redirect, THEN execute.

Claude Code's signature move is to lay out a numbered plan and work through it, with the
user able to steer before/while it runs. We already have steering (the interrupt channel);
this adds the missing half: a first turn that produces ONLY a plan and pauses, so the user
can approve it as-is or send an edit (which rides the existing steer mechanism into the
next turn).

This module is the pure/deterministic part: the prompt that asks for a plan, the marker
that says the plan is ready, and a parser that turns the reply into a list of steps. The
loop integration (pause after the plan, resume on approval/steer) lives in run_relay /
RelayWorker.
"""
from __future__ import annotations

import re

PLAN_READY = "PLAN_READY"

PLAN_PROMPT = (
    "【計画フェーズ】まだ実装を始めないでください。まずこのゴールの実行計画だけを作ります。\n"
    "必要なら read_file / grep / list_directory でリポジトリを下調べして構いませんが、"
    "ファイルの変更（write_file / replace_in_file）はまだ行わないこと。\n"
    "出力は番号付きの実行ステップ（各ステップ1行、上から順に実行する想定）。"
    "各ステップは具体的に（どのファイルに何をするか）。\n"
    "最後の行に必ず " + PLAN_READY + " と書いて、承認待ちで停止してください。\nGoal: "
)

APPROVE_JOB = (
    "計画を承認します。上記の計画に沿って、最初のステップから実際に実装を進めてください。"
    "各ターンの最後に CONTINUE / DONE / STUCK: 理由 のいずれかを書いてください。"
)

# a line that starts like a step: "1." / "1)" / "1、" / "- " / "* " / fullwidth "１." / "①"
_STEP_RE = re.compile(
    r"^\s*(?:[0-9]+[\.\)、]|[１-９]+[\.\)、]|[①-⑳]|[-*・])\s+(.+?)\s*$")
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def plan_ready(resp: str) -> bool:
    """True once the agent has finished the plan and is awaiting approval."""
    return PLAN_READY.upper() in (resp or "").upper()


def extract_plan(resp: str):
    """Parse the numbered/bulleted steps out of a plan reply. Returns a list of step
    strings (the PLAN_READY marker and any prose around the list are dropped)."""
    steps = []
    for line in (resp or "").splitlines():
        if PLAN_READY.upper() in line.upper():
            continue
        m = _STEP_RE.match(line)
        if m:
            step = m.group(1).strip()
            if step:
                steps.append(step)
    return steps
