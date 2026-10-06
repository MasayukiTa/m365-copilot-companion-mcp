# -*- coding: utf-8 -*-
"""A seven-way split produced seven conversation rows with one title between them.

`relay/conv_title.py` exists because 213 of 424 stored conversations were named after text
identical across unrelated tasks -- "the sidebar shows runs of visually identical rows". It
fixes that by deriving the title from the GOAL rather than from Copilot's boilerplate opening,
and it ships three more functions for the case where the goal does not separate rows either:
`repeated()`, `salvageable()`, `disambiguate()`.

Those three had no caller. `fleet_runner._register_convs` calls `make_title` once per row, in
isolation, and never looks at the list it has just built.

MEASURED 2026-09-13 on a real seven-way split:

    distinct titles: 1 of 7

Every child of a fan-out carries the PARENT's goal text -- `child_goals` puts it there
deliberately, because "a child runs in a conversation that has never seen the parent's" -- so
`make_title` truncates all seven to the same opening. The exact symptom conv_title.py was
written to remove, reproduced by the mechanism that became the default the same day.

WHAT THIS FIXES AND WHAT IT DOES NOT. After the de-duplication pass the seven rows are
distinguishable (7 of 7). They are not yet SELF-DESCRIBING: the tag says which row, not which
slice. `test_the_tag_identifies_the_row_but_not_yet_the_slice` states that limit out loud
rather than letting a later reader assume the problem is fully solved.
"""
from __future__ import annotations

import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import conv_title as ct   # noqa: E402
from relay import fanout as fo       # noqa: E402

PARENT = ("このリポジトリの全テストを通し、失敗をゼロにする。"
          "対象は relay/ tools/ scripts/ で、CIの6ゲートすべてを回すこと。")
STEPS = ["relay/ を通す", "tools/ を通す", "scripts/ を通す",
         "tests/ を通す", "bench/ を通す", "ui/ を通す", "統合する"]


def _entries():
    now = time.time()
    kids = fo.child_goals(PARENT, STEPS)
    return [{"title": ct.make_title(k["text"], key="k%d" % i, when=now),
             "url": "https://example.invalid/c/%d" % i, "ts": now}
            for i, k in enumerate(kids)]


def _dedupe(entries):
    """The pass `fleet_runner._register_convs` performs, mirrored here.

    It lives inside a long function that needs a browser and a fleet to reach, so the rule is
    exercised here and `test_the_registration_performs_the_pass` pins that the caller still
    performs it. That split is the weak point of this file and is named rather than hidden.
    """
    dupes = ct.repeated([e["title"] for e in entries], min_count=2)
    counts = {}
    for e in entries:
        t = (e["title"] or "").strip()
        if t in dupes:
            counts[t] = counts.get(t, 0) + 1
    for e in entries:
        t = (e["title"] or "").strip()
        if t not in dupes:
            continue
        e["title"] = (ct.disambiguate(t, key=e["url"], when=e["ts"])
                      if ct.salvageable(t, counts[t])
                      else ct.neutral_title(key=e["url"], when=e["ts"]))
    return entries


# ── the defect ────────────────────────────────────────────────────────────────────────────

def test_a_split_produces_rows_that_would_all_look_the_same():
    """THE PREMISE, measured. If this ever stops holding, the pass below is solving nothing
    and this file should be re-derived rather than kept green."""
    titles = [e["title"] for e in _entries()]
    assert len(set(titles)) == 1, (
        "前提が崩れている: 子のタイトルが既に分かれている（%d/%d）" % (len(set(titles)), len(titles)))


def test_after_the_pass_every_row_is_distinguishable():
    entries = _dedupe(_entries())
    titles = [e["title"] for e in entries]
    assert len(set(titles)) == len(titles), (
        "同じタイトルの行が残っている（%d/%d）" % (len(set(titles)), len(titles)))


def test_the_part_that_identified_the_task_is_kept():
    """`disambiguate` exists because replacing a repeated title with a bare id throws away the
    one true thing the row had: "sympy" on twenty rows names the project, it is just not
    unique. A bare neutral title here would be a regression dressed as a fix."""
    entries = _dedupe(_entries())
    for e in entries:
        assert PARENT[:12] in e["title"], e["title"]


def test_a_title_repeated_past_the_ceiling_is_replaced_rather_than_tagged():
    """Above UNSALVAGEABLE_AT a title identifies nothing and tagging it just makes a longer
    string that still identifies nothing."""
    assert not ct.salvageable("Microsoft Copilot", ct.UNSALVAGEABLE_AT + 1)
    assert ct.salvageable(PARENT[:40], 3)


def test_a_run_of_unique_titles_is_left_alone():
    now = time.time()
    entries = [{"title": "まったく違う用事 %d" % i, "url": "u%d" % i, "ts": now}
               for i in range(4)]
    before = [e["title"] for e in entries]
    assert [e["title"] for e in _dedupe(entries)] == before


# ── what it does NOT fix ──────────────────────────────────────────────────────────────────

def test_the_tag_identifies_the_row_but_not_yet_the_slice():
    """STATED, NOT HIDDEN. The rows are now distinguishable; they are not self-describing.
    A reader sees seven rows with the same goal and different four-character tags, and still
    cannot tell which one is `tools/` without opening it. `subtask_index` is on the envelope
    and would say so -- that is the next step, not something this change delivers."""
    entries = _dedupe(_entries())
    tags = [e["title"].rsplit("·", 1)[-1].strip() for e in entries]
    assert len(set(tags)) == len(tags), "タグが重複している"
    # THE SLICE MARKER, NOT A WORD FROM THE STEP. The first version of this looked for
    # `step[:6]` in the title and fired on "relay/" -- which is in the PARENT goal ("対象は
    # relay/ tools/ scripts/"), not in the slice assignment. A probe that can match the parent
    # cannot say anything about the child.
    #
    # `child_goals` writes 「全体の i/n」 into each child, so that marker is what would make a
    # row self-describing. It is not in the title.
    marker = "/%d" % len(STEPS)
    assert not any(marker in e["title"] for e in entries), (
        "担当範囲（全体の i/n）がタイトルに入っている -- ならこの検査は古い。"
        "上の注記ごと書き直すこと")


# ── the caller still performs it ──────────────────────────────────────────────────────────

def test_the_registration_performs_the_pass():
    src = open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8").read()
    i = src.index("merged, changed = merge_conv_rows(existing, entries)")
    before = src[max(0, i - 2000):i]
    assert "_ct2.repeated(" in before, "登録時に重複検出を行っていない"
    assert "_ct2.salvageable(" in before and "_ct2.disambiguate(" in before
    assert "_ct2.neutral_title(" in before


def test_a_cosmetic_title_never_fails_the_registration():
    """The rows are the record of what ran. Losing them because a title could not be computed
    would be a far worse trade than a duplicate title."""
    src = open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8").read()
    i = src.index("_ct2.repeated(")
    tail = src[i:src.index("merged, changed = merge_conv_rows", i)]
    assert "except Exception:" in tail
