# -*- coding: utf-8 -*-
"""A store that remembers one fact eight times.

MEASURED, 2026-08-31, on the live store: 156 theme files, and this is one of them in full --

    - [DONE] 2の12乗はいくつか、数字だけ答えて — refuter#1: UPHELD  <!-- 2026-08-28 08:49 -->
    - [DONE] 2の12乗はいくつか、数字だけ答えて — refuter#1: UPHELD  <!-- 2026-08-28 08:30 -->
    ... six more, identical but for the timestamp

A theme's entries are primed into every later goal on that theme, and the index of all themes
is primed into every goal at all. That index measured 4,047 characters against a
1,401-character protocol -- nearly three times the instructions, carrying repetition.

Same shape as the volatile-field defect in relay_fleet.py's no-progress key, found the same
day: a per-line timestamp makes "the same thing again" compare as new, and every mechanism
built on "is this different" then reads as if it were.
"""
import os

import pytest

from relay import project_memory as M


def line(goal="2の12乗はいくつか", note="refuter#1: UPHELD", when="2026-08-28 08:49",
         outcome="DONE"):
    return "- [%s] %s — %s  <!-- authority=EXTERNAL_UNTRUSTED -->  <!-- %s -->" % (
        outcome, goal, note, when)


# ── the collapse ──────────────────────────────────────────────────────────────────────────

def test_entries_differing_only_in_time_are_one_entry():
    got = M.dedupe_entries([line(when="2026-08-28 08:49"), line(when="2026-08-28 08:30")])
    assert len(got) == 1


def test_the_real_file_collapses_to_one_line():
    """Eight entries, one fact."""
    eight = [line(when="2026-08-28 0%d:00" % i) for i in range(1, 9)]
    got = M.dedupe_entries(eight)
    assert len(got) == 1
    assert "x8" in got[0], "the repetition count must survive: %r" % got[0]


def test_the_newest_survives():
    newest = line(when="2026-08-28 09:00")
    got = M.dedupe_entries([newest, line(when="2026-08-28 08:00")])
    assert got[0].startswith(newest.split("<!-- 2026")[0].rstrip())


def test_a_different_outcome_is_a_different_entry():
    """DONE and STUCK on the same goal are two facts, and the second is the interesting one."""
    got = M.dedupe_entries([line(outcome="STUCK"), line(outcome="DONE")])
    assert len(got) == 2


def test_a_different_note_is_a_different_entry():
    got = M.dedupe_entries([line(note="refuter#1: UPHELD"),
                            line(note="refuter#1: REFUTED, off-by-one in the loop")])
    assert len(got) == 2


def test_a_different_goal_is_a_different_entry():
    got = M.dedupe_entries([line(goal="2の12乗はいくつか"), line(goal="2の10乗はいくつか")])
    assert len(got) == 2


def test_counts_accumulate_rather_than_reset():
    """A line already marked x3, seen again, is x4 -- not x2."""
    a = M.dedupe_entries([line(when="2026-08-28 01:00"), line(when="2026-08-28 02:00"),
                          line(when="2026-08-28 03:00")])
    again = M.dedupe_entries([line(when="2026-08-28 04:00")] + a)
    assert "x4" in again[0], again[0]


def test_order_is_preserved_for_distinct_entries():
    got = M.dedupe_entries([line(goal="c"), line(goal="b"), line(goal="a")])
    assert [g.split("] ", 1)[1].split(" —")[0] for g in got] == ["c", "b", "a"]


def test_blank_and_malformed_lines_are_dropped_not_kept():
    got = M.dedupe_entries(["", "   ", None, line()])
    assert len(got) == 1


# ── through record_task and load_notes ────────────────────────────────────────────────────

def test_the_same_work_recorded_twice_leaves_one_entry(tmp_path):
    d = str(tmp_path)
    for i in range(8):
        assert M.record_task("算数", "2の12乗はいくつか、数字だけ答えて", "DONE",
                             note="refuter#1: UPHELD", state_dir=d, ts=1000 + i * 60)
    body = open(M._theme_path(M._resolve("算数")[1], d), encoding="utf-8").read()
    entries = M._entry_lines(body)
    assert len(entries) == 1, "8 identical records left %d entries" % len(entries)
    assert "x8" in entries[0]


def test_the_cap_no_longer_evicts_real_entries_to_hold_repeats(tmp_path):
    """DEDUPE BEFORE THE CAP. Twenty slots filled by one fact is a theme that remembers
    nothing while looking full -- and because the cap keeps the NEWEST, the genuinely
    different entries are the ones it drops."""
    d = str(tmp_path)
    M.record_task("t", "the interesting one", "STUCK", note="cannot reach the DB",
                  state_dir=d, ts=1000)
    for i in range(30):
        M.record_task("t", "the boring one", "DONE", note="ok", state_dir=d, ts=2000 + i)
    entries = M._entry_lines(open(M._theme_path(M._resolve("t")[1], d), encoding="utf-8").read())
    joined = "\n".join(entries)
    assert "cannot reach the DB" in joined, "the one distinct entry was evicted by repeats"
    assert len(entries) == 2


def test_notes_read_back_are_deduped_even_from_a_file_written_before_this_existed(tmp_path):
    """The 156 files already on disk are not rewritten until their theme comes round again,
    so a fix that only applies on write leaves the measured problem in place."""
    d = str(tmp_path)
    os.makedirs(M._mem_dir(d), exist_ok=True)
    theme, slug = M._resolve("算数")
    legacy = ("---\ntheme: %s\nupdated: x\nentries: 8\n---\n\n# %s\n\n%s\n"
              % (theme, theme, "\n".join(line(when="2026-08-28 0%d:00" % i)
                                         for i in range(1, 9))))
    with open(M._theme_path(slug, d), "w", encoding="utf-8") as fh:
        fh.write(legacy)
    out = M.load_notes("算数", state_dir=d, include_index=False)
    assert out.count("2の12乗") == 1, "read back %d copies" % out.count("2の12乗")


# ── the index, which is primed into EVERY goal ────────────────────────────────────────────

def idx(*titles):
    return ["- [%s](%s.md) — 3件 / 最終 2026-08-28 10:00" % (t, t) for t in titles]


def test_related_themes_come_first():
    lines = idx("625の平方根はいくつか", "ansible の executor", "2の12乗はいくつか")
    got = M.rank_index_lines(lines, "ansible", "fixing a bug in ansible/ansible")
    assert "ansible" in got[0]


def test_unrelated_themes_are_dropped_when_something_related_exists():
    """MEASURED: for a worker fixing a bug in ansible, ONE of the forty index lines shared a
    single token with its goal, and the other thirty-nine -- one-shot arithmetic questions the
    theme key had turned into themes -- were primed into its first turn anyway."""
    lines = idx("ansible の executor", *["問%d はいくつか" % i for i in range(20)])
    got = M.prune_index_lines(lines, "ansible", "fixing a bug in ansible/ansible")
    assert any("ansible" in g for g in got)
    assert len(got) <= 1 + M._INDEX_RECENT_TAIL


def test_a_recency_tail_survives_so_discovery_is_still_possible():
    """The index exists so a worker DISCOVERS a neighbouring theme. A filter that shows only
    what already looks related can never surface anything new."""
    lines = idx("ansible の executor", "まったく別の作業A", "まったく別の作業B")
    got = M.prune_index_lines(lines, "ansible", "fixing ansible")
    assert len(got) > 1, "nothing but the obvious match survived"


def test_the_tail_applies_even_when_nothing_matches():
    """THE BRANCH THAT EXEMPTED THE MEASURED CASE. The first version kept everything when
    nothing matched, meaning to be cautious. load_notes filters the CURRENT theme's own line
    out of the index before pruning, so for the ansible worker the single matching line was
    already gone, `related` was empty, and all thirty-nine unrelated lines came back -- the
    exact case the function was written for. A conservative branch that exempts the case being
    fixed is not caution."""
    lines = idx(*["問%d はいくつか" % i for i in range(20)])
    got = M.prune_index_lines(lines, "ansible", "fixing a bug in ansible")
    assert len(got) == M._INDEX_RECENT_TAIL
    assert got == lines[:M._INDEX_RECENT_TAIL], "the tail must be the most recent"


def test_an_empty_goal_changes_nothing():
    lines = idx("a", "b", "c")
    assert M.prune_index_lines(lines, "", "") == lines
    assert M.rank_index_lines(lines, "", "") == lines


def test_japanese_themes_match_without_spaces():
    """Whitespace splitting produces one enormous token per Japanese phrase and matches
    nothing, so the tokeniser has to fall back to CJK bigrams."""
    lines = idx("社員名簿のフリガナを確定する作業", "ansible の executor")
    got = M.rank_index_lines(lines, "登録済みフリガナの正誤を判定する作業", "フリガナを判定して")
    assert "フリガナ" in got[0]


def test_ranking_never_invents_or_loses_a_line():
    lines = idx("a-theme", "b-theme", "c-theme")
    got = M.rank_index_lines(lines, "b-theme", "")
    assert sorted(got) == sorted(lines)


def test_a_theme_with_genuinely_different_entries_keeps_all_of_them(tmp_path):
    """The failure direction that would matter: collapsing things that are not the same."""
    d = str(tmp_path)
    for i in range(5):
        M.record_task("t", "goal %d" % i, "DONE", note="note %d" % i, state_dir=d, ts=1000 + i)
    entries = M._entry_lines(open(M._theme_path(M._resolve("t")[1], d), encoding="utf-8").read())
    assert len(entries) == 5
