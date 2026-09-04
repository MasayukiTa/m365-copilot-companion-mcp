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
    # A different folder has no ENTRIES of its own -- but it does see the INDEX. That is
    # deliberate and is the whole point of the two-layer shape: without the index every
    # theme is an island and the store never compounds, which is exactly the failure mode
    # this rewrite was for. Assert the useful invariant instead of the old strict one:
    # nothing from another folder's history leaks into this folder's own entry list.
    other = tempfile.mkdtemp(prefix="pm_other_")
    other_notes = load_notes(other, state_dir=sd)
    check("other_folder_has_no_entries_of_its_own",
          "このテーマ" not in other_notes)
    # THIS ONCE ASSERTED THE OPPOSITE, on an argument that was never measured: without the
    # index every theme is an island and the store never compounds. Sound in principle, and it
    # never happened -- .fleet/tool_events.jsonl holds 22,444 recorded tool calls in which
    # `.fleet/memory` appears ZERO times. No worker has ever opened a theme the index offered
    # it. What the block did do was carry noise: a cinema survey was primed with a furigana
    # task and two arithmetic one-shots, eight mentions of an unrelated subject in a
    # 3,646-character prompt.
    #
    # RELATED themes are still offered in full; only the unrelated filler is gone, which is
    # what an unrelated folder now correctly receives none of. See relay/test_memory_dedupe.py
    # for the positive case, and _INDEX_RECENT_TAIL for how to turn this back on if the recall
    # path is ever wired to something that reads it.
    check("an_unrelated_folder_is_not_primed_with_other_themes",
          "記憶している他のテーマ" not in other_notes)
    check("index_only_when_something_is_remembered",
          load_notes(tempfile.mkdtemp(prefix="pm_empty_"),
                     state_dir=tempfile.mkdtemp(prefix="pm_nostate_")) == "")

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
