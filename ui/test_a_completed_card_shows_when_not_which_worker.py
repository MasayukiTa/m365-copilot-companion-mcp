# -*- coding: utf-8 -*-
"""A COMPLETED card says WHEN it finished, not WHICH worker ran it -- source-level checks.

## The requirement

"完了済みのカードは、担当ワーカーの番号（W67 のような識別子）ではなく、タスクの題名と
完了した時刻を見せてほしい。並びも、最近完了したものが上に来るようにして。"

## What this asserts

The WPF render itself needs a live GUI, so (as with the other cockpit source tests -- see
`test_goal_summary_display.py`) this pins the three decisions at the SOURCE level:

1. `SubmittedTasks.CompletedUnix` reads the ts of the LAST phase_event (the completion
   instant), mirroring `CreatedUnix` which reads the FIRST.
2. `SubmittedTasks.Compose` orders the terminal bucket by `CompletedUnix`, newest first, and
   does so stably (an index breaks ties so equal/zero completion ts keeps status.json order).
3. The collapsed card's meta line, on a terminal card, appends the completion time ("完了 HH:mm")
   and does NOT append the worker name -- `name.ToUpper()` stays on the live (else) branch only.

`SubmittedTasks.cs` is NOT a FleetCockpit* part, so it is read directly; the cockpit meta line
lives in a FleetCockpit* part and is read through `tools.cockpit_source`.

The ORDER behaviour is additionally executed end-to-end (real csc + harness) by
`test_a_submitted_task_is_on_top_at_once.py`.
"""
import os

from tools import cockpit_source  # only for its pure member() text slicer

UI = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    # Read the file FROM THIS WORKING TREE. tools.cockpit_source.read() resolves paths
    # relative to the installed package (a different checkout), so the cockpit meta line is
    # read by name here instead -- the completion-time branch lives in FleetCockpit.Card.cs.
    with open(os.path.join(UI, name), encoding="utf-8-sig") as fh:
        return fh.read()


def _submitted_src():
    return _read("SubmittedTasks.cs")


def _cockpit_src():
    return _read("FleetCockpit.Card.cs")


# ── CompletedUnix: the LAST phase_event's ts ────────────────────────────────────────

def test_completed_unix_reads_the_last_phase_event_ts():
    s = _submitted_src()
    assert "public static double CompletedUnix(" in s, "CompletedUnix helper is missing"
    b = cockpit_source.member(s, "public static double CompletedUnix(")
    # last element, both array shapes the first-event helper already handles
    assert "arr[arr.Length - 1]" in b, b
    assert "al[al.Count - 1]" in b, b
    # reads ts off that element, same key CreatedUnix uses
    assert 'd.TryGetValue("ts", out ts)' in b, b


# ── Compose: terminal bucket newest-completion first, stable ──────────────────────────

def test_compose_orders_the_terminal_bucket_by_completion_newest_first():
    s = _submitted_src()
    b = cockpit_source.member(s, "public static List<SubmittedDisplayItem> Compose(")
    # active + older pending still emit in status.json order (ranks 1..2 only)
    assert "for (int rank = 1; rank <= 2; rank++)" in b, b
    # terminal (rank 3) is pulled out and sorted by CompletedUnix, b before a == descending
    assert "x.Rank == 3" in b, b
    assert "CompletedUnix(b.Worker).CompareTo(CompletedUnix(a.Worker))" in b, b
    # stable: an index dictionary breaks ties
    assert "doneIdx" in b, b
    assert "doneIdx[a].CompareTo(doneIdx[b])" in b, b
    # the old unconditional 1..3 status.json emission is gone
    assert "for (int rank = 1; rank <= 3; rank++)" not in b, \
        "terminal cards must no longer emit in plain status.json order"


# ── the collapsed meta line: when, not which ─────────────────────────────────────

def _meta_block(s):
    i = s.index("var meta = new StringBuilder();")
    return s[i:s.index("var ml = new TextBlock", i)]


def test_the_meta_line_shows_completion_time_on_a_terminal_card():
    b = _meta_block(_cockpit_src())
    assert "if (terminal)" in b, b
    assert "SubmittedTasks.CompletedUnix(w)" in b, b
    assert 'dt.ToString("HH:mm")' in b, b
    assert '"完了 " + hhmm' in b, b
    assert '"completed " + hhmm' in b, b


def test_the_meta_line_hides_the_worker_number_on_a_terminal_card():
    b = _meta_block(_cockpit_src())
    # the worker name is appended ONLY on the live (else) branch, never for terminal
    assert "meta.Append(name.ToUpper());" in b, b
    pre = b[:b.index("meta.Append(name.ToUpper());")]
    # the nearest control-flow keyword before the name append is `else`, not `if (terminal)`
    assert pre.rfind("else") > pre.rfind("if (terminal)"), \
        "name.ToUpper() must sit on the else (live) branch, not the terminal one"


def test_the_meta_line_never_leads_with_a_stray_separator():
    s = _cockpit_src()
    assert 'if (metaText.StartsWith(" · ")) metaText = metaText.Substring(3);' in s, \
        "the leading-separator trim is missing"
