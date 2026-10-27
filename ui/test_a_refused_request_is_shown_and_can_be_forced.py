# -*- coding: utf-8 -*-
"""A request fleet_submit turned away is SEEN in the cockpit and can be FORCED in.

## The requirement

fleet_submit refuses some goals (a near-duplicate, a pasted document, a full queue) and, until
now, the only sign of that was a sentence returned to the CALLER -- the agent over the tunnel.
The person at the machine saw nothing: the Cockpit's submitted list is built from the queue and
status.json, and a refused goal is in neither. The owner's rule is that the system must not
silently drop a request. So:

  * the submitted area draws a "投入されなかった依頼" (refused) card for each refused record;
  * the card states the reason IN JAPANESE -- for a near-duplicate, naming the look-alike job
    and the measured overlap as a percent -- and carries a "強制投入" (force) button;
  * the button runs the operator-only override (tools.fleet_intake --force-refused <id>), which
    is deliberately NOT an MCP tool, so only a person at the machine reaches it.

And separately (same file, ui/FleetCockpit.Submit.cs): when the GUI "開始" button fails to start
a new run, the REASON is now put on screen, not just the exit code.

## What this runs

Nothing compiles here. These are source assertions over the SHIPPED C# -- the same technique
ui/test_every_lane_in_flight_is_on_screen.py uses -- so they run on the Linux CI host with no
csc and no WPF. The cockpit parts (FleetCockpit*.cs) are read through tools/cockpit_source so a
part added later is still covered; ui/SubmittedTasks.cs is read directly, because it is not a
FleetCockpit part and cockpit_source does not include it.

Comments are STRIPPED before the negative checks: the reasoning paragraphs in the source quote
the very strings under test, and a raw substring scan would match the comment instead of the
code (the trap ui/test_every_lane_in_flight_is_on_screen.py documents).
"""
from __future__ import annotations

import os
import re

from tools import cockpit_source

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUBMITTED_TASKS = os.path.join(REPO, "ui", "SubmittedTasks.cs")


def _cockpit_src():
    return cockpit_source.read(encoding="utf-8")


def _submitted_tasks_src():
    with open(SUBMITTED_TASKS, encoding="utf-8-sig") as fh:
        return fh.read()


def _strip_line_comments(src):
    """Drop // line comments (and whole-line /// doc comments) so a negative assertion sees code,
    not the prose that explains it. Block comments are rare in this source and left as-is."""
    out = []
    for ln in src.splitlines():
        s = ln.lstrip()
        if s.startswith("//"):
            continue
        out.append(ln.split("//", 1)[0] if "//" in ln else ln)
    return "\n".join(out)


# -- the refused card exists and is wired into the submitted area ----------------------------

def test_the_submitted_area_appends_a_refused_row_per_record():
    """BuildRows must emit the refused cards (row kind 10) in the top area, in every state --
    not just the empty state, or a refused request vanishes the moment a run is live."""
    src = _strip_line_comments(_cockpit_src())
    assert "AppendRefusedRows(" in src, "BuildRows never appends the refused cards"
    assert "void AppendRefusedRows(" in src, "the refused-row builder is missing"
    # it reads the records through the panel's published list
    assert "ReadRefusedCards()" in src


def test_row_kind_10_maps_to_the_refused_card_builder():
    """The virtualizing list's converter must turn a kind-10 row into a RefusedCard, or the row
    model exists but nothing draws it."""
    src = _strip_line_comments(_cockpit_src())
    assert "RefusedCard(" in src, "the refused-card builder is missing"
    assert re.search(r"r\.Kind\s*==\s*10", src), "the converter has no case for row kind 10"


def test_a_spent_refused_record_is_dropped_so_the_button_does_not_linger():
    """ReadRefusedCards must skip a record already forced in -- a spent card with a live button
    would double-submit on a second press."""
    src = _cockpit_src()
    body = cockpit_source.member(src, "List<RefusedView> ReadRefusedCards()")
    body = _strip_line_comments(body)
    assert ".Forced" in body, "ReadRefusedCards does not consult the forced flag"


# -- the card shows the reason in Japanese, with the look-alike and the overlap --------------

def test_the_reason_line_is_japanese_and_names_the_lookalike_and_overlap():
    """RefusedReasonText is where the 'why, and against what' is composed. For a near-duplicate
    it must be Japanese, name the similar job, and show the measured overlap as a percent."""
    src = _submitted_tasks_src()
    body = cockpit_source.member(src, "public static string RefusedReasonText(")
    # near-duplicate: Japanese reason + the look-alike + a percent '一致'
    assert "重複のため却下" in body
    assert "% 一致" in body
    assert "v.SimilarTo" in body, "the reason does not name the job it looked like"
    assert "v.Similarity" in body, "the reason does not use the measured overlap"
    # the other refusal kinds also have Japanese text, so no card ever shows a bare reason code
    for ja in ("長すぎるため却下", "待機列が一杯", "空の目標のため却下"):
        assert ja in body


def test_the_similarity_is_rendered_as_a_whole_percent():
    """0..1 overlap -> 'NN% 一致'. A test that the percent is computed from the stored value,
    not left as a fraction the operator has to read."""
    src = _submitted_tasks_src()
    body = cockpit_source.member(src, "public static string RefusedReasonText(")
    assert "Similarity * 100" in body.replace(" ", "") or "Similarity*100" in body.replace(" ", "")


def test_the_force_button_is_labelled_in_japanese():
    src = _strip_line_comments(_cockpit_src())
    body = cockpit_source.member(_cockpit_src(), "UIElement RefusedCard(")
    assert "強制投入" in body, "the force button has no Japanese label"
    # the whole goal is on the card (its tooltip is the full goal), not just a head
    assert "v.Goal" in body


# -- the force button runs the operator-only override, off the MCP surface -------------------

def test_the_force_button_invokes_the_operator_cli_not_an_mcp_tool():
    """The override is reached by launching tools.fleet_intake --force-refused <id>, which is
    force_submit_refused -- an entry kept out of main.py's TOOLS. The button must go through
    that CLI, carrying the refused-record id, so the capability stays person-only."""
    src = _cockpit_src()
    body = cockpit_source.member(src, "void ForceRefusedGoal(")
    body_code = _strip_line_comments(body)
    assert "tools.fleet_intake" in body_code
    assert "--force-refused" in body_code
    assert "refusedId" in body_code, "the force path does not pass the refused-record id"
    # and the card's button actually calls it
    card = _strip_line_comments(cockpit_source.member(src, "UIElement RefusedCard("))
    assert "ForceRefusedGoal(" in card


# -- the GUI 'Start' failure now says WHY, on screen -----------------------------------------

def test_a_failed_start_puts_the_reason_on_screen_not_just_a_code():
    """The defect: a run that failed to start showed only an exit code (or nothing). The runner's
    own last stderr line is now captured and shown, so the person sees the reason."""
    src = _cockpit_src()
    # the reason extractor exists and is static (a test could feed it a buffer)
    reason = cockpit_source.member(src, "static string StartFailureReason(")
    assert "errBuf" in reason, "the failure-reason helper does not read the captured stderr"
    # stderr is actually redirected and drained, or there is nothing to show
    code = _strip_line_comments(src)
    assert "RedirectStandardError = true" in code
    assert "BeginErrorReadLine()" in code
    # and the start note appends the reason, in Japanese, after the exit code
    assert "理由" in src, "the start note never appends a reason"
    assert "StartFailureReason(" in code, "the failure path never calls the reason helper"
