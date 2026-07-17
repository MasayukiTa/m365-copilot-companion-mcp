"""Tests for plan-first execution: planner parsing + the worker's plan/await/approve flow.

No browser. Proves the plan phase pauses for approval and a steer (approve or edit)
resumes into normal execution -- the Claude-Code plan-then-do loop, built on the existing
steering channel.

Run:  .venv\\Scripts\\python.exe relay\\test_planner.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.planner import PLAN_PROMPT, extract_plan, plan_ready
from relay.relay_fleet import RelayWorker

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def settle(w, states, steps=200):
    for _ in range(steps):
        if w.status not in states:
            return
        w.poll()


def main():
    # --- pure parser ---
    check("plan_ready_true", plan_ready("steps...\nPLAN_READY"))
    check("plan_ready_false", not plan_ready("still drafting"))
    plan = extract_plan("計画:\n1. calc.py を読む\n2) add を修正\n③ テストを足す\n- 仕上げ\nPLAN_READY")
    check("extract_numbered", "calc.py を読む" in plan and "add を修正" in plan)
    check("extract_circled_bullet", "テストを足す" in plan and "仕上げ" in plan)
    check("extract_drops_marker", all("PLAN_READY" not in s for s in plan))
    check("extract_count", len(plan) == 4)

    # unmarked paragraph plan (one step per line under a '...:' header) -- the REAL format
    # the live agent used (no numbers). Steps after the header are kept; the agent-name
    # preamble before it and the PLAN_READY line are dropped.
    unmarked = ("takeuchifile操作\n実行計画:\n"
                "fibo.py を replace_in_file で修正する。\n"
                "shell_exec で pytest -q を実行して確認する。\n"
                "失敗したら出力を分析して再修正する。\nPLAN_READY")
    up = extract_plan(unmarked)
    check("extract_unmarked", len(up) == 3 and "takeuchifile操作" not in " ".join(up)
          and any("replace_in_file" in s for s in up))

    # --- worker plan flow ---
    w = RelayWorker("g", "w0", plan_mode=True)
    check("plan_initial_job", w.job.startswith("[") and PLAN_PROMPT[:10] in w.job
          and w.goal in w.job)

    # turn 1 returns a plan -> pause for approval
    w._decide("1. step one\n2. step two\nPLAN_READY")
    check("awaiting_after_plan", w.status == "awaiting" and not w._plan_approved)
    check("plan_captured", w.plan_steps == ["step one", "step two"])

    # no steer yet -> stays awaiting (paused)
    settle(w, ("awaiting",), steps=5)
    check("stays_paused", w.status == "awaiting")

    # a steer (approval / edit) resumes execution
    w.steer("承認。計画通り実行して")
    w.poll()
    check("resumes_on_steer", w.status == "ready" and w._plan_approved)

    # after approval the flow is normal: DONE (no checks, refuter off) -> done
    w._decide("done now DONE")
    check("executes_after_approval", w.status == "done" and w.outcome == "DONE")

    # incomplete plan (no PLAN_READY) -> nudged to finish, stays in the loop
    w2 = RelayWorker("g", "w1", plan_mode=True)
    w2._decide("考え中です。少々お待ちを")
    check("incomplete_plan_nudged", w2.status == "ready" and "PLAN_READY" in w2.job
          and not w2._plan_approved)

    # plan_mode off -> normal PROTOCOL goal, no awaiting
    w3 = RelayWorker("g", "w2")
    check("nonplan_unchanged", "awaiting" != w3.status and w3.plan_mode is False)

    print("\n=== %d/%d planner checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
