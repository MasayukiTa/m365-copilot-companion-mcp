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

# a line that starts like a step. Tolerant of: "1." / "1)" / "1、" / "1：" / fullwidth
# "１．" / "①" / "- " / "* " / "・" and an optional "Step N" / "ステップN" prefix; the
# separator and the space after a circled/bullet marker are optional (agents vary).
_STEP_RE = re.compile(
    r"^\s*(?:(?:step|ステップ)\s*)?"
    r"(?:[0-9]+[\.\)、：．:]|[０-９]+[\.\)、：．:]|[①-⑳]|[-*・])\s*(.+?)\s*$",
    re.IGNORECASE)


def _clean_step(s):
    """Drop leading markdown emphasis / colons so '**calc.py を読む**:' -> 'calc.py を読む'."""
    return s.strip().strip("*").strip().rstrip(":：").strip()


def plan_ready(resp: str) -> bool:
    """True once the agent has finished the plan and is awaiting approval."""
    return PLAN_READY.upper() in (resp or "").upper()


def extract_plan(resp: str):
    """Parse the steps out of a plan reply. Returns a list of step strings (the PLAN_READY
    marker and surrounding prose dropped).

    Agents vary: some number/bullet the steps, some write one plain line per step under a
    header like '実行計画:'. So we first take any marked steps; if there are none, we fall
    back to the substantial lines that follow a '...:' header (each line = one step)."""
    lines = (resp or "").splitlines()
    steps = []
    for line in lines:
        if PLAN_READY.upper() in line.upper():
            continue
        m = _STEP_RE.match(line)
        if m:
            step = _clean_step(m.group(1))
            if step:
                steps.append(step)
    if steps:
        return steps
    # fallback: unmarked, one step per line. Collect substantial lines AFTER a header line
    # that ends with ':' / '：' (e.g. '実行計画:'), which also skips an agent-name preamble.
    collecting = False
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if PLAN_READY.upper() in s.upper():
            break
        if s.endswith(":") or s.endswith("："):
            collecting = True
            continue
        if collecting:
            s2 = _clean_step(s)
            if len(s2) >= 8:
                steps.append(s2)
    return steps
