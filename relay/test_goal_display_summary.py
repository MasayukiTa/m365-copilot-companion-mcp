# -*- coding: utf-8 -*-
"""The execution goal stays complete; the operator-facing goal is a short task identity."""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from relay import conv_title as ct
from relay import fleet_runner as fr


def _w(goal):
    return SimpleNamespace(
        name="w0", goal=goal, status="running", outcome="", turn=1, max_turns=9,
        reason="", closed=False, conv_url="", conv_title="", verified=None,
        verify_attempts=0, last_response="", transcript="", cwd="", phase_events=[],
        task_id="", parent_task_id=None, campaign_id="", role="", depth=0,
        goal_hash="", fresh_replay_count=0, refusal_count=0, refusal_history=[],
        recovery_cause="", recovery_result="", recovery_state="", attempt_transcripts=[],
        page=None,
        tab_load=lambda: 0,
    )


def test_policy_only_opening_is_skipped_for_task_identity():
    a = ("READ-ONLY audit only; do not edit, commit, push, reset, or mutate repository files. "
         "Review commits abc123 and def456 plus the current main tree. Thoroughly search for regressions.")
    b = ("READ-ONLY investigation only; do not edit, delete, commit, reset, or mutate repository files. "
         "Thoroughly investigate the unlock retry and requeue regressions in C:/repo/project.")
    ta = ct.make_title(a)
    tb = ct.make_title(b)
    assert ta.startswith("Review commits"), ta
    assert tb.startswith("Thoroughly investigate"), tb
    assert len(ta) <= ct.MAX_LEN + 1 and len(tb) <= ct.MAX_LEN + 1


def test_snapshot_has_short_summary_but_keeps_full_goal_exactly():
    goal = ("READ-ONLY audit only; do not edit, commit, push, reset, or mutate repository files. "
            "Review the session recovery implementation and find remaining races. " + "detail " * 300)
    snap = fr._snapshot([_w(goal)], 1.0, 1, directive=goal, run_label="legacy")
    row = snap["workers"][0]
    assert row["goal"] == goal
    assert row["goal_summary"].startswith("Review the session recovery")
    assert len(row["goal_summary"]) <= ct.MAX_LEN + 1
    assert snap["directive"] == goal
    assert snap["directive_summary"] == row["goal_summary"]


def test_final_snapshot_builder_carries_goal_summary():
    src = Path(fr.__file__).read_text(encoding="utf-8")
    i = src.index("def _final_worker_entry")
    block = src[i:i + 3300]
    assert '"goal_summary": _goal_summary(r["goal"])' in block
    final = src[src.index('final = {"started"'):src.index('final = {"started"') + 900]
    assert '"directive_summary": _goal_summary(directive) if directive else ""' in final


def test_run_label_is_the_task_summary_not_a_verbatim_prefix():
    src = Path(fr.__file__).read_text(encoding="utf-8")
    main = src[src.index("def main():"):]
    assert "run_label = _goal_summary(gtexts[0]) if gtexts else \"\"" in main
    assert "_first_line[:60]" not in main
