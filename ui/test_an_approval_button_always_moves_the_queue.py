# -*- coding: utf-8 -*-
"""Pressing 承認 on a gate whose file is gone did nothing, and kept doing nothing.

REPORTED 2026-09-12 with a screenshot -- "これ承認押した後ずっと消えない" -- against a window
showing a pending `Approve delete: <a scratch file>?` and `7件待機`.

    void Answer(string verdict)
    {
        ...
        var gate = ReadGate(_currentPath); if (gate == null) return;

`ReadGate` returns null when the file is missing or unreadable. That `return` left `_current`
and `_currentPath` set and never reached `LoadNext()`, so the window kept displaying a request
it could no longer act on and every further press took the same path. The only escape was
closing the window.

THE SAME SHAPE SAT ONE BLOCK LOWER: `catch { return; }` around the write. A locked file, a full
disk, or a failed replace-after-copy left the identical dead state -- and worse, silently, while
the operator believed they had approved something.

WHY IT MATTERS BEYOND THE ANNOYANCE: a button that does nothing teaches people the window is
broken, and after that they stop reading what it says. That is precisely the failure an
approval queue exists to prevent, and it is the same reason the manual approval mode was
demoted from "recommended" the same day.

WHAT THE TWO CASES MEAN, kept distinct:
  * file gone       -- answered or withdrawn elsewhere (console, another window, a cleanup).
                       A resolved request. Move on quietly; announcing something the operator
                       did themselves is noise.
  * write failed    -- the operation they just approved will NOT proceed, because whatever
                       reads the answer never sees it. They have to be told.
"""
from __future__ import annotations

import os
import re

UI = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(UI, "FleetCockpit.cs")


def _code() -> str:
    """The source with // comments stripped -- the comments below name every identifier this
    file asserts on, so without this the module would pass on its own explanations."""
    with open(SRC, encoding="utf-8") as fh:
        text = fh.read()
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in text.splitlines())


def _answer_body(code: str) -> str:
    i = code.index("void Answer(string verdict)")
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[i:j + 1]
    return code[i:i + 4000]


def test_no_path_out_of_answer_skips_the_queue():
    """THE DEFECT. Every `return` inside Answer must be preceded by LoadNext(), or the window
    keeps showing a request it cannot act on."""
    body = _answer_body(_code())
    # the guard clause that rejects a high-impact confirmation is allowed to return early --
    # nothing has changed and the same gate is still the right thing to show.
    guarded = body.split("MessageBoxResult.No) != MessageBoxResult.Yes) return;", 1)[-1]
    for m in re.finditer(r"\breturn\b", guarded):
        before = guarded[:m.start()]
        assert "LoadNext();" in before[-220:], (
            "a return in Answer() does not advance the queue; the window will keep showing "
            "the same request: ...%s" % before[-160:].strip())


def test_a_vanished_gate_is_treated_as_resolved():
    body = _answer_body(_code())
    assert "if (gate == null)" in body, "a missing gate file is no longer handled explicitly"
    i = body.index("if (gate == null)")
    assert "LoadNext();" in body[i:i + 260], (
        "a gate whose file is gone does not advance the queue -- this is the reported defect")


def test_a_failed_write_tells_the_operator():
    """The two cases must not be collapsed: a write that failed means the operation they just
    approved will not happen, and silence there is worse than silence about a vanished file."""
    body = _answer_body(_code())
    assert "catch (Exception ex)" in body, "the write failure is still swallowed untyped"
    i = body.index("catch (Exception ex)")
    tail = body[i:i + 700]
    assert "MessageBox.Show" in tail, "a failed write is silent"
    assert "LoadNext();" in tail, "a failed write leaves the queue stuck"


def test_the_quiet_case_stays_quiet():
    """A request the operator answered elsewhere is not an error, and telling them about it
    every time is how a queue becomes noise."""
    body = _answer_body(_code())
    i = body.index("if (gate == null)")
    assert "MessageBox" not in body[i:i + 260], (
        "a vanished gate now pops a dialog; the operator resolved it themselves")


def test_the_deployed_cockpit_carries_this_fix():
    """A source assertion cannot see a stale build, and this window is the one the operator is
    actually pressing. The string is unique to the failed-write path added here."""
    import pytest
    exe = os.path.join(UI, "FleetCockpit.exe")
    if not os.path.isfile(exe):
        pytest.skip("no built binary here (CI)")
    with open(exe, "rb") as fh:
        blob = fh.read()
    needle = "この承認を記録できませんでした".encode("utf-16-le")
    assert needle in blob, (
        "FleetCockpit.exe predates this fix -- rebuild it with ui/rebuild_ui.ps1, or the "
        "running window still freezes on a gate it cannot answer")
