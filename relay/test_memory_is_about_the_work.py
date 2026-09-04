# -*- coding: utf-8 -*-
"""The store accumulated 974 entries and almost none of them said anything.

Measured over the live .fleet/memory at 974 entries across 168 themes:

    refuter#1: UPHELD          618   63.4%   one repeated string
    retry bookkeeping           26    2.7%
    goal text restated                59%    of every stored character

The rest was mostly more of the same in a shape the first pass did not match --
"previous turn still generating -> wait 124s/360s", "goal not received by agent ->
resend goal 1/3", "8 個のサブタスクに分割して並列実行" -- plus answers to one-shot questions
("15, 30, 45, 60, 75, 90 DONE") that no later task will ever ask again.

The cause is the write site: it passes `w.reason or w.last_response`, and `reason` is the
frame's own status field, so plumbing wins whenever it is set.

This mattered beyond the waste. Fourteen themes sat pinned at the twenty-entry cap, and the
cap keeps the NEWEST -- so a theme full of repeated refuter verdicts was evicting the entries
that had something in them.

Replaying the whole live store through the new write path: 974 entries become 473, the store
shrinks 56%, and themes pinned at the cap fall from 14 to 2.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import project_memory as M  # noqa: E402

THEME = "You are fixing a real bug in the open-source project **tutao"
GOAL = (THEME + "/tutanota** (language: ts). The repository is checked out locally at: "
                "C:/Users/x/checkout")


# -- what must never be stored as a note ---------------------------------------------------

@pytest.mark.parametrize("note", [
    "refuter#1: UPHELD",
    "refuter#2: OVERTURNED",
    "STUCK -> transient retry 1/10",
    "not re-sent: the turn may already have been delivered (delivery=unknown)",
    "previous turn still generating -> wait 124s/360s (no budget)",
    "goal not received by agent -> resend goal 1/3",
    "agent reported STUCK (after 9 retries)",
    "8 個のサブタスクに分割して並列実行（完了後に統合）",
    "ConnectionClosedOK: received 1000 (OK); then sent 1000 (OK)",
])
def test_the_harness_talking_about_itself_is_not_a_memory(note):
    assert M.is_frame_plumbing(note), note


@pytest.mark.parametrize("note", [
    "V_製品検査データ が正規の MCL 検査ビュー。他の名前は別物",
    "the failing assert is in tests/test_parser.py::test_nested, not in the parser",
    "MAXDOP 1 kills the parallel read-ahead; that is the real mechanism",
    "pip needs --trusted-host here; the corporate TLS proxy breaks verification",
])
def test_something_the_work_discovered_is_kept(note):
    assert not M.is_frame_plumbing(note), note


def test_a_task_genuinely_about_retrying_can_still_say_so():
    """The pattern is matched against the NOTE, never the goal, so work on the retry logic
    itself is still recordable."""
    assert not M.is_frame_plumbing("the backoff was reading seconds as milliseconds")


def test_an_empty_note_is_not_plumbing():
    assert not M.is_frame_plumbing("") and not M.is_frame_plumbing(None)


# -- the entry that gets written ------------------------------------------------------------

def test_a_plumbing_note_still_records_that_the_work_happened(tmp_path):
    """The outcome is the part worth keeping. Dropping the whole entry would lose the fact
    that this theme was attempted and how it ended."""
    assert M.record_task(THEME, GOAL, "DONE", note="refuter#1: UPHELD",
                         state_dir=str(tmp_path))
    text = _theme_file(tmp_path)
    assert "[DONE]" in text
    assert "refuter" not in text


def test_repeated_plumbing_collapses_instead_of_filling_the_cap(tmp_path):
    """THE REASON THIS MATTERS. 618 identical refuter lines were filling twenty-entry themes,
    and the cap keeps the newest -- so they evicted the entries that said something."""
    for _ in range(30):
        M.record_task(THEME, GOAL, "DONE", note="refuter#1: UPHELD", state_dir=str(tmp_path))
    M.record_task(THEME, GOAL, "DONE", note="the fix belongs in src/parser.ts, not the test",
                  state_dir=str(tmp_path))
    lines = [l for l in _theme_file(tmp_path).splitlines() if l.startswith("- [")]
    assert len(lines) <= 3, "the repeats did not collapse: %d lines" % len(lines)
    assert any("src/parser.ts" in l for l in lines), "the entry with content was evicted"


def test_the_substantive_note_survives_a_flood_of_plumbing(tmp_path):
    M.record_task(THEME, GOAL, "DONE", note="V_製品検査データ が正規ビュー",
                  state_dir=str(tmp_path))
    for i in range(40):
        M.record_task(THEME, GOAL, "STUCK", note="transient retry %d/10" % i,
                      state_dir=str(tmp_path))
    assert "V_製品検査データ" in _theme_file(tmp_path)


# -- the goal restatement ---------------------------------------------------------------------

def test_the_heading_is_not_repeated_on_every_line(tmp_path):
    """59% of every stored character was the goal, under a heading that already named it."""
    M.record_task(THEME, GOAL, "DONE", note="ok", state_dir=str(tmp_path))
    line = [l for l in _theme_file(tmp_path).splitlines() if l.startswith("- [")][0]
    assert THEME not in line
    assert "/tutanota" in line, "the part that distinguishes this goal was lost too"


def test_what_distinguishes_two_goals_under_one_heading_is_kept(tmp_path):
    a = THEME + "/tutanota** (language: ts)"
    b = THEME + "/other-repo** (language: go)"
    M.record_task(THEME, a, "DONE", note="one", state_dir=str(tmp_path))
    M.record_task(THEME, b, "DONE", note="two", state_dir=str(tmp_path))
    text = _theme_file(tmp_path)
    assert "/tutanota" in text and "/other-repo" in text


def test_the_tail_is_capped():
    got = M._note_body(THEME + "/x" + ("y" * 400), THEME)
    assert len(got) <= M._TAIL_CAP


def test_a_goal_that_is_not_a_prefix_of_its_theme_is_left_alone():
    """A caller that passes its own theme, unrelated to the goal text, must not be trimmed
    against it."""
    got = M._note_body("count the files in docs/", "ブリッジの同意カード")
    assert got == "count the files in docs/"


def test_a_goal_identical_to_its_theme_stores_nothing_extra():
    assert M._note_body(THEME, THEME) == ""


def _theme_file(tmp_path):
    d = os.path.join(str(tmp_path), "memory")
    names = [f for f in os.listdir(d) if f.endswith(".md") and f != M._INDEX_NAME]
    assert len(names) == 1, names
    with open(os.path.join(d, names[0]), encoding="utf-8") as fh:
        return fh.read()
