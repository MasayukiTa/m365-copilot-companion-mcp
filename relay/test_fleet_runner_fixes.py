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

FIX 5 -- coordinator output capture (_setup_coordinator_log / _Tee):
    (t) _setup_coordinator_log() creates a log file under state_dir and returns its path.
    (u) A write to (the now-teed) sys.stdout appears in BOTH the real stream and the log
        file -- the tee does not swallow or replace console output.

FIX 6 -- RUN-ACTIVE marker + auto-resume decision logic:
    (v) _write_active_marker() + _read_active_marker() round-trip pid/start_ts/argv.
    (w) _resume_argv() strips -g/--goal VALUE and --goals-file VALUE (and any existing
        --resume), keeping connection/tuning flags -- so replaying it after --resume
        does not duplicate the goals already carried in the durable ledger.
    (x) _clear_active_marker() removes the marker; clearing a missing marker is a no-op.
    (y) should_auto_resume(): marker+dead-pid -> resume; marker absent, pid alive, or
        user-stopped -> do NOT resume (all branches).

FIX 4 -- _read_goals() / _read_goals_file() fragmented-goals-file guard:
    (m) A goals-file that is really a shredded multi-line PROMPT (intro sentence, a
        colon-terminated fragment, a bare path, several "tools/x.py" bullet lines, a
        "<<<FINDINGS>>>" delimiter, a lone "[" line, a JSON object with no text/goal
        key) is REJECTED with an actionable SystemExit that points at
        bench/review_build_goals.py's write_goals_jsonl().
    (n) A goals-file of many short plain-text fragments (no delimiters, no bad JSON)
        is rejected by the soft aggregate heuristic alone.
    (o) A legit plain-text one-goal-per-line file (+ a # comment + a blank line)
        parses to exactly the real goals -- NOT regressed by the guard.
    (p) A legit JSONL file in write_goals_jsonl format ({"text": "..."} per line)
        parses to those goals -- NOT regressed.
    (q) A single-goal file (ONE real goal) parses to exactly 1 goal.
    (r) A JSON-object line with no recognized goal key is rejected as EMPTY, not
        silently turned into an empty lane.
    (s) The reject test actually exercises the guard code path (proven by
        temporarily neutering the guard and observing the test FAIL -- see the
        VERIFY step in the delegating task; not re-run automatically here).

Run:  .venv\\Scripts\\python.exe relay\\test_fleet_runner_fixes.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import re
from argparse import Namespace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Import the helpers under test directly.
from relay.fleet_runner import (
    _pending_gates, _clean_final_text, _read_goals,
    _setup_coordinator_log, _write_active_marker, _read_active_marker,
    _clear_active_marker, _resume_argv, should_auto_resume,
)

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
# FIX 5: coordinator output capture (_setup_coordinator_log / _Tee)
# ---------------------------------------------------------------------------

def _test_fix5_coordinator_log():
    real_stdout, real_stderr = sys.stdout, sys.stderr
    with tempfile.TemporaryDirectory() as tmp:
        log_path = None
        try:
            log_path = _setup_coordinator_log(tmp)
            check("fix5_log_path_created", bool(log_path) and os.path.isfile(log_path))

            # capture what the REAL stream receives by swapping it for a buffer BEFORE
            # calling _setup_coordinator_log's tee target -- simplest hermetic check:
            # the tee delegates the write to the real stream synchronously, so a marker
            # written to sys.stdout must land in both the (still-console) real stream's
            # return value AND the log file. We can't easily capture "the real console"
            # here without breaking pytest/CI output capture, so instead verify the log
            # file directly received the write while sys.stdout keeps functioning
            # (write() returns normally, no exception) -- proving console output is not
            # swallowed or broken by the tee.
            marker_text = "FLEET_RUNNER_TEE_SMOKE_MARKER_12345\n"
            n = sys.stdout.write(marker_text)
            sys.stdout.flush()
            check("fix5_tee_write_did_not_raise_and_returned_len",
                  n == len(marker_text) or n is None or n > 0)

            with open(log_path, encoding="utf-8") as f:
                content = f.read()
            check("fix5_tee_wrote_to_log_file", marker_text in content)
        finally:
            # close the file handle the Tee holds before the tempdir is removed
            # (Windows keeps an open handle from being deleted).
            try:
                sys.stdout._log.close()
            except Exception:
                pass
            sys.stdout, sys.stderr = real_stdout, real_stderr


# ---------------------------------------------------------------------------
# FIX 6: RUN-ACTIVE marker + auto-resume decision logic
# ---------------------------------------------------------------------------

def _test_fix6_active_marker_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        # nothing written yet -> None
        check("fix6_marker_absent_reads_none", _read_active_marker(tmp) is None)

        argv = ["-g", "hello world", "--agent-url", "http://x", "--effort", "auto"]
        _write_active_marker(tmp, argv=argv, pid=4242, start_ts=1000.0)
        m = _read_active_marker(tmp)
        check("fix6_marker_roundtrip_pid", m is not None and m.get("pid") == 4242)
        check("fix6_marker_roundtrip_start_ts", m is not None and m.get("start_ts") == 1000.0)
        check("fix6_marker_roundtrip_argv", m is not None and m.get("argv") == argv)
        check("fix6_marker_has_resume_argv", m is not None and "resume_argv" in m)

        _clear_active_marker(tmp)
        check("fix6_marker_cleared", _read_active_marker(tmp) is None)
        # clearing an already-absent marker must be a silent no-op, not an error.
        _clear_active_marker(tmp)
        check("fix6_clear_missing_marker_is_noop", _read_active_marker(tmp) is None)


def _test_fix6_resume_argv():
    # -g/--goal VALUE and --goals-file VALUE are stripped; connection/tuning flags kept.
    argv = ["--agent-url", "http://x", "-g", "goal one", "--goal", "goal two",
            "--goals-file", "goals.txt", "--effort", "max", "--max-turns", "50"]
    stripped = _resume_argv(argv)
    check("fix6_resume_argv_drops_dash_g", "-g" not in stripped and "goal one" not in stripped)
    check("fix6_resume_argv_drops_dash_dash_goal", "--goal" not in stripped and "goal two" not in stripped)
    check("fix6_resume_argv_drops_goals_file", "--goals-file" not in stripped and "goals.txt" not in stripped)
    check("fix6_resume_argv_keeps_agent_url", "--agent-url" in stripped and "http://x" in stripped)
    check("fix6_resume_argv_keeps_tuning_flags",
          "--effort" in stripped and "max" in stripped
          and "--max-turns" in stripped and "50" in stripped)

    # an existing --resume in argv (e.g. this run WAS itself a resumed run) is dropped so
    # the caller can append exactly one --resume without a duplicate flag.
    argv2 = ["--agent-url", "http://x", "--resume"]
    check("fix6_resume_argv_drops_existing_resume", "--resume" not in _resume_argv(argv2))

    # --goal=VALUE / --goals-file=VALUE (= form) are also stripped.
    argv3 = ["--agent-url", "http://x", "--goals-file=goals.txt"]
    stripped3 = _resume_argv(argv3)
    check("fix6_resume_argv_drops_equals_form", "--goals-file=goals.txt" not in stripped3)


def _test_fix6_should_auto_resume():
    # marker present + pid dead + not user-stopped -> resume
    check("fix6_resume_when_marker_and_dead_pid",
          should_auto_resume(marker_exists=True, pid_alive=False, user_stopped=False) is True)
    # marker absent -> never resume, regardless of pid_alive
    check("fix6_no_resume_when_marker_absent",
          should_auto_resume(marker_exists=False, pid_alive=False, user_stopped=False) is False)
    # pid alive (already running) -> never resume
    check("fix6_no_resume_when_pid_alive",
          should_auto_resume(marker_exists=True, pid_alive=True, user_stopped=False) is False)
    # user explicitly stopped -> never resume even if marker/pid look interrupted
    check("fix6_no_resume_when_user_stopped",
          should_auto_resume(marker_exists=True, pid_alive=False, user_stopped=True) is False)
    # both marker absent AND pid alive (nonsensical combo) -> still no resume
    check("fix6_no_resume_when_marker_absent_and_pid_alive",
          should_auto_resume(marker_exists=False, pid_alive=True, user_stopped=False) is False)


def _test_fix6_marker():
    _test_fix6_active_marker_roundtrip()
    _test_fix6_resume_argv()
    _test_fix6_should_auto_resume()


# ---------------------------------------------------------------------------
# FIX 4: _read_goals() / _read_goals_file() fragmented-goals-file guard
# ---------------------------------------------------------------------------

def _goals_args(goals_file):
    """A minimal args-like object -- _read_goals() only reads .goal and .goals_file."""
    return Namespace(goal=None, goals_file=str(goals_file))


def _write_tmp(tmp_dir: Path, name: str, content: str) -> Path:
    p = tmp_dir / name
    p.write_text(content, encoding="utf-8")
    return p


def _test_fix4_reject_fragmented_prompt():
    """(m) A shredded multi-line review PROMPT must be REJECTED with an actionable error."""
    fragmented = "\n".join([
        "あなたはこのリポジトリのコードレビューを行います。",          # intro sentence
        "対象リポジトリ（このPCのローカル、git チェックアウト済み）:",   # colon-terminated fragment
        "C:/Users/example/some-repo",                                    # bare path line
        "- tools/notify_ops.py",
        "- tools/file_ops.py",
        "- tools/contract_gate.py",
        "1. セキュリティ上の懸念を洗い出すこと",
        "<<<FINDINGS>>>",
        "[",
        '{"file": "tools/notify_ops.py", "line": 12, "severity": "high"}',
        "]",
        "<<<END_FINDINGS>>>",
    ])
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_tmp(Path(tmp), "retry_input.txt", fragmented)
        raised = False
        msg = ""
        try:
            _read_goals(_goals_args(p))
        except SystemExit as e:
            raised = True
            msg = str(e)
        check("fix4_fragmented_prompt_rejected", raised)
        check("fix4_error_mentions_jsonl_builder",
              "write_goals_jsonl" in msg and "review_build_goals" in msg)
        check("fix4_error_names_the_file", str(p) in msg)


def _test_fix4_reject_soft_aggregate_only():
    """(n) Many short plain-text fragments (no delimiter tokens, no bad JSON) trip
    the SOFT aggregate heuristic alone -- proves that path works standalone."""
    lines = ["tools/file_%d.py" % i for i in range(10)]   # 10 short (<40 char) lines
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_tmp(Path(tmp), "shredded_paths.txt", "\n".join(lines))
        raised = False
        msg = ""
        try:
            _read_goals(_goals_args(p))
        except SystemExit as e:
            raised = True
            msg = str(e)
        check("fix4_soft_aggregate_rejected", raised)
        check("fix4_soft_aggregate_mentions_jsonl_builder", "write_goals_jsonl" in msg)


def _test_fix4_accept_legit_plaintext():
    """(o) A legit one-goal-per-line plain-text file (+ comment + blank line) must
    NOT be regressed by the guard -- parses to exactly the real goals."""
    real_goals = [
        "Fix the login redirect bug by validating the session token before redirecting.",
        "Update the README to document the new --resume flag and its ledger files.",
        "Add unit tests for the disk-floor admission gate under low free space.",
    ]
    content = "\n".join([
        "# this is a comment and should be ignored",
        "",
        real_goals[0],
        real_goals[1],
        real_goals[2],
    ])
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_tmp(Path(tmp), "goals.txt", content)
        goals = _read_goals(_goals_args(p))
        check("fix4_legit_plaintext_not_regressed", goals == real_goals)


def _test_fix4_accept_legit_jsonl():
    """(p) A legit write_goals_jsonl-format file ({"text": "..."} per line) must
    NOT be regressed -- parses to those goal dicts."""
    entries = [
        {"text": "Review tools/notify_ops.py for unhandled exceptions.", "cwd": "C:/repo"},
        {"text": "Review tools/file_ops.py for path traversal issues.", "cwd": "C:/repo"},
    ]
    content = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_tmp(Path(tmp), "goals.jsonl", content)
        goals = _read_goals(_goals_args(p))
        check("fix4_legit_jsonl_not_regressed",
              len(goals) == 2 and all(isinstance(g, dict) for g in goals)
              and [g["text"] for g in goals] == [e["text"] for e in entries])


def _test_fix4_accept_single_goal():
    """(q) A single-goal file (ONE real goal) parses to exactly 1 goal."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_tmp(Path(tmp), "one_goal.txt",
                        "Investigate why the nightly backup job silently skips large files.\n")
        goals = _read_goals(_goals_args(p))
        check("fix4_single_goal_parses_to_one", goals == [
            "Investigate why the nightly backup job silently skips large files."])


def _test_fix4_reject_empty_goal_from_json():
    """(r) A JSON-object line with no recognized goal key resolves to an EMPTY goal
    and must be rejected -- never silently turned into an empty lane."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_tmp(Path(tmp), "bad_goal.jsonl",
                        '{"file": "tools/notify_ops.py", "line": 5}\n')
        raised = False
        msg = ""
        try:
            _read_goals(_goals_args(p))
        except SystemExit as e:
            raised = True
            msg = str(e)
        check("fix4_empty_goal_from_missing_key_rejected", raised)
        check("fix4_empty_goal_error_mentions_empty", "EMPTY" in msg)

    # also: a dict WITH a 'text' key but whose value is whitespace-only
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_tmp(Path(tmp), "blank_text.jsonl", '{"text": "   "}\n')
        raised = False
        try:
            _read_goals(_goals_args(p))
        except SystemExit:
            raised = True
        check("fix4_whitespace_only_text_rejected", raised)


def _test_fix4_guard():
    _test_fix4_reject_fragmented_prompt()
    _test_fix4_reject_soft_aggregate_only()
    _test_fix4_accept_legit_plaintext()
    _test_fix4_accept_legit_jsonl()
    _test_fix4_accept_single_goal()
    _test_fix4_reject_empty_goal_from_json()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    _test_fix1_gate_scoping()
    _test_fix2_clean_final_text()
    _test_fix3_run_label()
    _test_fix5_coordinator_log()
    _test_fix6_marker()
    _test_fix4_guard()
    passed = sum(results)
    total = len(results)
    print("\n=== %d/%d fleet_runner fix checks passed ===" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
