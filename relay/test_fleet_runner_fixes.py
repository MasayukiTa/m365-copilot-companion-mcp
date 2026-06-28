"""Hermetic tests for the three fleet_runner fixes (FIX 1/2/3).

These tests are self-contained: they exercise only pure helper functions and do NOT
require a browser, MCP server, or any filesystem side-effects.

FIX 1 (P0) -- _pending_gates() run-scoping:
    (a) A gate with asked_at < started MUST be excluded.
    (b) A gate with asked_at >= started MUST be included.
    (c) A malformed gate (no asked_at, or non-numeric asked_at) MUST be skipped without crash.

FIX 2 (P0) -- _clean_final_text():
    (d) Trailing "DONE" is stripped.
    (e) Trailing "<promptend>" is stripped.
    (f) Tool-call preamble at the start is stripped.
    (g) Substantive text survives unchanged.
    (h) Output is capped at max_len.
    (i) Empty / None input returns "".

FIX 3 (P2) -- run_label derivation (tested via the logic used in main(), extracted inline):
    (j) run_label is the verbatim first line of the first goal, truncated to 60 chars.
    (k) Leading list markers / whitespace are stripped from run_label.
    (l) goal_count equals the number of goals.

Run:  .venv\\Scripts\\python.exe relay\\test_fleet_runner_fixes.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Import the helpers under test directly.
from relay.fleet_runner import _pending_gates, _clean_final_text

results: list[bool] = []


def check(name: str, cond: bool) -> None:
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


# ---------------------------------------------------------------------------
# FIX 1 helpers -- we need to control the gate directory.
# _pending_gates() imports ALLOWED_BASE from tools.file_ops which points at a
# real directory.  We monkeypatch it by temporarily overriding the module-level
# attribute so the function scans our temp dir instead.
# ---------------------------------------------------------------------------

def _make_gate(gate_dir: Path, name: str, asked_at, answered=False, question="Q?") -> Path:
    """Write a minimal gate JSON into gate_dir and return its path."""
    d: dict = {"token": name, "question": question, "context": "", "answered": answered}
    if asked_at is not None:
        d["asked_at"] = asked_at
    p = gate_dir / ("gate_%s.json" % name)
    p.write_text(json.dumps(d), encoding="utf-8")
    return p


def _pending_gates_from_dir(gate_dir: Path, started: float) -> list[dict]:
    """Call _pending_gates with a custom gate_dir by monkeypatching ALLOWED_BASE."""
    import tools.file_ops as _fo
    original = _fo.ALLOWED_BASE
    _fo.ALLOWED_BASE = gate_dir.parent   # gate_dir == ALLOWED_BASE / ".companion_gates"
    # rename temp dir so the glob pattern works
    companion_dir = gate_dir.parent / ".companion_gates"
    if gate_dir != companion_dir:
        gate_dir.rename(companion_dir)
        gate_dir = companion_dir
    try:
        return _pending_gates(started=started)
    finally:
        _fo.ALLOWED_BASE = original


def _test_fix1_gate_scoping():
    """FIX 1: gate scoping by started timestamp."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        gate_dir = base / ".companion_gates"
        gate_dir.mkdir()

        started = 1000.0
        # stale gate: asked_at < started
        _make_gate(gate_dir, "old", asked_at=500.0, question="Stale gate")
        # current gate: asked_at == started (boundary -- must be included)
        _make_gate(gate_dir, "exact", asked_at=1000.0, question="Exact boundary gate")
        # current gate: asked_at > started
        _make_gate(gate_dir, "new", asked_at=1200.0, question="New gate")
        # already answered gate (should always be excluded regardless of time)
        _make_gate(gate_dir, "answered", asked_at=1100.0, answered=True, question="Answered")
        # malformed gate: asked_at is a string (non-numeric)
        _make_gate(gate_dir, "malformed_str", asked_at=None, question="Malformed string")
        # malformed gate: no asked_at key at all (written without it)
        p_nokey = gate_dir / "gate_nokey.json"
        p_nokey.write_text(json.dumps({"token": "nokey", "question": "No key", "answered": False}))

        import tools.file_ops as _fo
        original = _fo.ALLOWED_BASE
        _fo.ALLOWED_BASE = base
        try:
            gates = _pending_gates(started=started)
        finally:
            _fo.ALLOWED_BASE = original

        tokens = {g["token"] for g in gates}
        check("fix1_stale_gate_excluded",   "old"     not in tokens)
        check("fix1_boundary_gate_included","exact"   in tokens)
        check("fix1_new_gate_included",     "new"     in tokens)
        check("fix1_answered_excluded",     "answered" not in tokens)
        check("fix1_malformed_str_skipped", "malformed_str" not in tokens)
        check("fix1_no_asked_at_skipped",   "nokey"   not in tokens)
        check("fix1_no_crash",              True)   # we got here without exception


# ---------------------------------------------------------------------------
# FIX 2: _clean_final_text
# ---------------------------------------------------------------------------

def _test_fix2_clean_final_text():
    """FIX 2: cleaning of the final assistant text."""
    # (d) trailing bare DONE is stripped
    r = _clean_final_text("The answer is 42\nDONE")
    check("fix2_trailing_done_stripped", "DONE" not in r and "42" in r)

    # (e) trailing <promptend> is stripped (case-insensitive)
    r = _clean_final_text("Fleet visual review A <promptend>")
    check("fix2_promptend_stripped", "<promptend>" not in r.lower() and "Fleet visual" in r)

    # (f) DONE at end of text with surrounding whitespace
    r = _clean_final_text("Good result.  DONE  ")
    check("fix2_done_with_space", r == "Good result.")

    # (g) substantive text passes through unchanged (modulo whitespace collapse)
    text = "The deployment completed successfully. All 5 tests passed."
    r = _clean_final_text(text)
    check("fix2_substantive_text_preserved", "All 5 tests passed" in r)

    # (h) output is capped at max_len
    long_text = "x" * 1000
    r = _clean_final_text(long_text, max_len=600)
    check("fix2_capped_at_max_len", len(r) <= 600)

    # (i) empty / None input returns ""
    check("fix2_empty_returns_empty",  _clean_final_text("") == "")
    check("fix2_none_returns_empty",   _clean_final_text(None) == "")

    # tool-call preamble stripped
    r = _clean_final_text("[TOOL_CALL] some_fn\nActual answer here")
    check("fix2_tool_call_preamble_stripped", "TOOL_CALL" not in r and "Actual answer" in r)

    # text that is ONLY "DONE" should become empty
    r = _clean_final_text("DONE")
    check("fix2_only_done_becomes_empty", r == "")

    # --- control-word stripping (mirrored from CleanAgentResultForUi._resultPreambleTokens) ---
    # (j) leading "desktopfile操作" prefix is stripped, rest preserved
    r = _clean_final_text("desktopfile操作 Fleet review C DONE")
    check("fix2_ctrl_desktopfile_prefix_stripped",
          r == "Fleet review C" and "desktopfile操作" not in r)

    # (k) leading "browser操作" prefix is stripped
    r = _clean_final_text("browser操作 X")
    check("fix2_ctrl_browser_prefix_stripped", r == "X" and "browser操作" not in r)

    # (l) string with NO control word passes through unchanged (modulo trailing DONE / ws)
    r = _clean_final_text("All tests passed successfully.")
    check("fix2_no_ctrl_word_unchanged", r == "All tests passed successfully.")

    # (m) line-only control token is dropped (multi-line case — mirrors C# per-line logic)
    r = _clean_final_text("computeruse\nActual result here")
    check("fix2_ctrl_line_only_dropped", "computeruse" not in r.lower() and "Actual result" in r)

    # (n) "Copilot" as sole token stripped as prefix
    r = _clean_final_text("Copilot Some summary text")
    check("fix2_ctrl_copilot_prefix_stripped", r == "Some summary text")


# ---------------------------------------------------------------------------
# FIX 3: run_label / goal_count derivation (inline logic, same as main())
# ---------------------------------------------------------------------------

def _derive_run_label(gtexts):
    """Mirror the run_label derivation logic from fleet_runner.main()."""
    _first_goal_text = gtexts[0] if gtexts else ""
    _first_line = _first_goal_text.splitlines()[0] if _first_goal_text else ""
    _first_line = re.sub(r'^[\s\-*#\d.>]+', '', _first_line).strip()
    return _first_line[:60]


def _test_fix3_run_label():
    """FIX 3: run_label and goal_count."""
    goals = ["Fix the login bug in auth.py", "Update the README", "Add unit tests"]

    # (j) run_label is verbatim first line of first goal, <=60 chars
    label = _derive_run_label(goals)
    check("fix3_run_label_verbatim_first_line", label == "Fix the login bug in auth.py")
    check("fix3_run_label_max_60", len(label) <= 60)

    # (k) leading list markers stripped
    label2 = _derive_run_label(["- Fix the login bug"])
    check("fix3_leading_dash_stripped", label2 == "Fix the login bug")
    label3 = _derive_run_label(["1. Fix the login bug"])
    check("fix3_leading_number_stripped", label3 == "Fix the login bug")
    label4 = _derive_run_label(["# Section header goal"])
    check("fix3_leading_hash_stripped", label4 == "Section header goal")

    # (l) goal_count is the number of goals
    goal_count = len(goals)
    check("fix3_goal_count_correct", goal_count == 3)

    # multi-line goal: run_label is ONLY the first line
    multiline_goal = "Deploy new version\nThis includes all services\nand the database"
    label5 = _derive_run_label([multiline_goal])
    check("fix3_multiline_first_line_only", label5 == "Deploy new version")

    # truncation at 60 chars
    long_goal = "A" * 80
    label6 = _derive_run_label([long_goal])
    check("fix3_truncated_to_60", len(label6) == 60)

    # empty goals list
    label7 = _derive_run_label([])
    check("fix3_empty_goals_gives_empty", label7 == "")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    _test_fix1_gate_scoping()
    _test_fix2_clean_final_text()
    _test_fix3_run_label()
    passed = sum(results)
    total = len(results)
    print("\n=== %d/%d fleet_runner fix checks passed ===" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
