"""Tests for persistent per-folder project memory (auto-accumulated across tasks).

Run:  .venv\\Scripts\\python.exe relay\\test_project_memory.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.code_task import build_goal
from relay.project_memory import load_notes, record_task

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def main():
    sd = tempfile.mkdtemp(prefix="pm_state_")
    folder = tempfile.mkdtemp(prefix="pm_proj_")

    # empty -> no notes
    check("empty_no_notes", load_notes(folder, state_dir=sd) == "")

    # record + load round-trip
    record_task(folder, "落ちてるテストを直す", "DONE", note="calc.add を + に修正", state_dir=sd, ts=1)
    record_task(folder, "型ヒント追加", "DONE", note="calc.py に hint", state_dir=sd, ts=2)
    notes = load_notes(folder, state_dir=sd)
    check("notes_present", "落ちてるテストを直す" in notes and "型ヒント追加" in notes)
    check("notes_newest_first", notes.index("型ヒント追加") < notes.index("落ちてるテストを直す"))
    check("notes_have_outcome_and_note", "[DONE]" in notes and "calc.add を + に修正" in notes)

    # different folder is isolated
    other = tempfile.mkdtemp(prefix="pm_other_")
    check("folder_isolated", load_notes(other, state_dir=sd) == "")

    # cap at 20 most recent
    for i in range(25):
        record_task(folder, "task %d" % i, "DONE", state_dir=sd, ts=100 + i)
    full = load_notes(folder, max_items=99, state_dir=sd)
    check("cap_keeps_recent", "task 24" in full and "task 4" not in full)

    # build_goal primes the notes into the task text
    record_task(folder, "前回: バグ修正", "DONE", note="重要メモ", state_dir=sd, ts=999)
    goal, gnotes = build_goal("新しい作業", folder, with_map=False, state_dir=sd)
    check("build_goal_primes_memory", "過去の作業" in goal["text"] and "前回: バグ修正" in goal["text"])
    check("build_goal_notes_flag", any("project memory" in n for n in gnotes))

    # with_memory off -> no priming
    goal2, _ = build_goal("x", folder, with_map=False, with_memory=False, state_dir=sd)
    check("build_goal_memory_off", "過去の作業" not in goal2["text"])

    print("\n=== %d/%d project-memory checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
