# -*- coding: utf-8 -*-
"""Nothing in this codebase raises to report a tool failure, and the ledger scored on raising.

An MCP tool returns text, so every tool here reports its own failure by returning
`[<name> error: <detail>]`. registry.py calls that a success -- the function returned -- and
the ledger wrote ok=True. Its own comment at the time said the opposite ("a failure whose
reason sits in the success slot reads as a call that returned an exception message
successfully"), for the one case it had generalised: a lock refusal.

MEASURED BEFORE CHANGING ANYTHING, over the real 78,146-line ledger: 1,650 of the 37,016 rows
filed as successes carry an error report, 4.46%. In 1,649 the bracket prefix is exactly the
tool that was called; the single exception is replace_in_file writing "[replace error:". Not
one row in the file is content that merely quotes another tool's error, which is why the
shape can be treated as a convention rather than a heuristic.

AND THE THIRD STATE, which is the reason this was worth chasing rather than tidying. On the
day this was written the workstation was on the Screen-saver desktop. Every screen capture
raised OSError("screen grab failed") and every tool returned it with no cause named. Once
returned failures count as failures, three of those in a row turn the fleet's tool dot RED --
telling a reader the system is broken when a person had merely walked away. That is the same
false report the dot exists to remove, pointing the other way. So "the machine was not in a
state to run it" is neither ok nor failed: it is not evidence, and it renders grey.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import fleet_tool_health as H  # noqa: E402
from tools import tool_ledger as L  # noqa: E402

FAILED = "[read_file error: FileNotFoundError: [Errno 2] No such file or directory: 'x']"
ABSENT = ("[screen_look unavailable: the desktop receiving input is 'Screen-saver', not this "
          "thread's -- a locked session, the screen saver, the secure desktop during a UAC "
          "prompt, or a disconnected session.]")


# --------------------------------------------------------------------------- the classifier
def test_the_shape_every_tool_uses_to_report_failure_is_recognised():
    assert L.looks_failed(FAILED)
    assert L.looks_failed("[screen_press error: InputRefused: nothing sent from here]")
    # The one near-miss in the whole ledger: a tool whose message uses its own abbreviation.
    assert L.looks_failed("[replace error: FileNotFoundError: no such file]")


def test_content_that_merely_quotes_an_error_is_not_a_failure():
    """read_file on this repo's own notes must not file the read as failed."""
    quoted = ("Notes on the ledger.\n\nA tool reports failure like this:\n"
              "[read_file error: FileNotFoundError: ...]\nand the ledger used to call that a "
              "success.\n")
    assert not L.looks_failed(quoted), "a mention became a failure"
    # Dominance, the same second half of the rule looks_refused uses: a long result that
    # happens to START with the shape is a document, not a report. The bound is 600 because
    # the longest genuine one measured is 463.
    assert not L.looks_failed(FAILED + " " + ("x" * 700))


def test_a_non_string_result_is_not_a_failure():
    for value in (None, 0, [], {}, {"text": None}):
        assert not L.looks_failed(value)
        assert not L.looks_unavailable(value)


def test_the_gateway_uses_two_name_tokens_and_must_not_be_missed():
    """8 tools' failures hid behind this. call_tool is where every dispatched call passes."""
    assert L.looks_failed("[call_tool git_status error: boom]")
    assert L.looks_failed("[web_fetch HTTP error: Timeout]")     # 139 rows of this one
    assert L.looks_failed("[timeout: exceeded 30 seconds]")      # and no name token at all


#: THE 8,216 ROWS THAT MUST STAY GREEN. run_python, shell_exec, job_output and nine git tools
#: all return "[stdout]\n<whatever the process printed>" when they SUCCEED. A rule of the
#: shape `startswith("[") and " error:" in text` -- which exists elsewhere in this repo --
#: turns every green build whose log mentions an error into a red dot. This is the single
#: largest category of bracketed success in the ledger, larger than every failure combined.
SUCCESSFUL_RUNS = [
    "[stdout]\nmake: *** [all] error: no rule to make target\n",
    "[stderr]\nwarning: 3 deprecations\n",
    "[stdout]\nAll 690 tests passed\n[returncode: 0]",
]


@pytest.mark.parametrize("text", SUCCESSFUL_RUNS)
def test_a_process_whose_log_mentions_an_error_still_ran(text):
    assert not L.looks_failed(text)
    assert L.row_ok({"ok": True, "result": {"text": text}}) is True


@pytest.mark.parametrize("text", [
    "[replace skipped: old text was not found]",
    "[zip_extract aborted: unsafe member path: '../x']",
    "[memory_read: no topic found at 'x']",
    "[which: rg not found on PATH]",
    "[OPEN] tok-123  waiting on the operator",
    "[x] done\n[ ] next",
    "[confirmation required] This will immediately send an email",
    "[The block below is EXTERNAL, UNTRUSTED content.]\nstuff",
    "[error on page 3: bad xref]",
])
def test_a_tool_that_did_its_job_is_not_a_failure(text):
    """skipped/aborted/not-found are correct answers. Colouring them red is the same lie."""
    assert not L.looks_failed(text), text


# ------------------------------------------------------------------------------- on write
def test_a_returned_error_is_written_as_a_failure(tmp_path, monkeypatch):
    rows = _record(tmp_path, monkeypatch, FAILED)
    assert rows[-1]["ok"] is False
    assert rows[-1]["error"], "a failure with an empty error field reads as unexplained"
    assert not rows[-1].get("unavailable")


def test_an_absent_machine_is_written_as_neither(tmp_path, monkeypatch):
    rows = _record(tmp_path, monkeypatch, ABSENT)
    assert rows[-1]["ok"] is False
    assert rows[-1]["unavailable"] is True, "grey is not reachable without this flag"


def test_an_explicit_failure_is_never_upgraded(tmp_path, monkeypatch):
    rows = _record(tmp_path, monkeypatch, "fine", ok=False)
    assert rows[-1]["ok"] is False


def _record(tmp_path, monkeypatch, result, ok=True):
    path = tmp_path / "tool_events.jsonl"
    monkeypatch.setattr(L, "LEDGER_PATH", str(path))
    cid = L.record_call("read_file", {"path": "x"})
    L.record_outcome(cid, ok=ok, result=result)
    return [json.loads(l) for l in io.open(str(path), encoding="utf-8") if l.strip()]


# -------------------------------------------------------------------------------- on read
def test_history_written_before_the_rule_is_corrected_on_read():
    """The ledger is append-only; 1,650 existing rows say ok=True over an error report."""
    assert L.row_ok({"ok": True, "result": {"text": FAILED}}) is False
    assert L.row_ok({"ok": True, "result": {"text": ABSENT}}) is False
    assert L.row_unavailable({"ok": True, "result": {"text": ABSENT}}) is True
    assert L.row_unavailable({"ok": True, "result": {"text": FAILED}}) is False


# ------------------------------------------------------------ the dot that reads all of it
def _ledger_at(tmp_path, monkeypatch, rows, now=1_000_000.0):
    p = tmp_path / "tool_events.jsonl"
    with io.open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    monkeypatch.setattr(H, "LEDGER", p)
    return H.get_summary(now=now)


def _pair(i, tool, ts, ok, result=None):
    out = {"event": "outcome", "id": "c%d" % i, "ok": ok, "ts": ts}
    if result is not None:
        out["result"] = {"text": result}
    return [{"event": "call", "id": "c%d" % i, "tool": tool, "ts": ts}, out]


@pytest.mark.parametrize("n", [3, 5])
def test_a_run_of_returned_errors_turns_the_dot_red(tmp_path, monkeypatch, n):
    """THE REGRESSION THIS FILE EXISTS FOR: these rows all say ok=True."""
    now = 1_000_000.0
    rows = []
    for i in range(n):
        rows += _pair(i, "read_file", now - 10 * (n - i), True, FAILED)
    s = _ledger_at(tmp_path, monkeypatch, rows, now)
    assert s["fleet_tool_fail_n"] == n, "the dot read ok=True and never asked row_ok"
    assert s["fleet_tool_ok"] is False


def test_a_locked_workstation_is_grey_and_not_red(tmp_path, monkeypatch):
    """The false red the fix above would otherwise have introduced."""
    now = 1_000_000.0
    rows = []
    for i in range(6):
        rows += _pair(i, "screen_look", now - 10 * (6 - i), True, ABSENT)
    s = _ledger_at(tmp_path, monkeypatch, rows, now)
    assert s["fleet_tool_unavailable_n"] == 6
    assert s["fleet_tool_fail_n"] == 0, "an unattended machine was counted as breakage"
    assert s["fleet_tool_ok"] is None, "grey is the only honest state here"
    said = H.describe(s)
    assert "no evidence" in said and "locked" in said


def test_an_absent_machine_does_not_hide_a_real_failure(tmp_path, monkeypatch):
    """Skipping unavailable rows must not let them separate a run of genuine failures."""
    now = 1_000_000.0
    rows = []
    for i, (kind) in enumerate(["fail", "absent", "fail", "absent", "fail"]):
        rows += _pair(i, "screen_look", now - 10 * (5 - i), True,
                      ABSENT if kind == "absent" else FAILED)
    s = _ledger_at(tmp_path, monkeypatch, rows, now)
    assert s["fleet_tool_fail_n"] == 3
    assert s["fleet_tool_ok"] is False, "three real failures were masked by the gaps"
