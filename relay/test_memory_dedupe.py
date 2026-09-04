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


def test_the_read_path_is_left_to_the_memory_component(tmp_path):
    """THE ARM I NEARLY DELETED BY IMPLEMENTING IT.

    Collapsing repeats at READ time is what `memory/v2` already does -- a declared component
    with two arms, whose comparison is the benchmark's job. The first version of this work
    deduped on the read path unconditionally, which made v1 and v2 return byte-identical text
    and turned the experiment into two runs of one program. That is precisely the failure
    MEMORY_VERSIONS exists to end, and it was caught by
    relay/selfimprove/test_evolution_loop.py rather than by me.

    So a legacy file full of repeats still primes repeats under v1, and does not under v2.
    Both are correct; which is better is not this function's decision.
    """
    d = str(tmp_path)
    os.makedirs(M._mem_dir(d), exist_ok=True)
    theme, slug = M._resolve("算数")
    legacy = ("---\ntheme: %s\nupdated: x\nentries: 8\n---\n\n# %s\n\n%s\n"
              % (theme, theme, "\n".join(line(when="2026-08-28 0%d:00" % i)
                                         for i in range(1, 9))))
    with open(M._theme_path(slug, d), "w", encoding="utf-8") as fh:
        fh.write(legacy)
    assert M._memory_v1(M._entry_lines(legacy), 8) != M._memory_v2(M._entry_lines(legacy), 8)


def test_the_write_path_still_dedupes_because_that_is_a_different_question(tmp_path):
    """v2 filters what is PRIMED; it cannot recover an entry the cap already evicted. Whether
    a distinct entry survives at all is decided when it is written."""
    d = str(tmp_path)
    M.record_task("t", "the interesting one", "STUCK", note="cannot reach the DB",
                  state_dir=d, ts=1000)
    for i in range(30):
        M.record_task("t", "the boring one", "DONE", note="ok", state_dir=d, ts=2000 + i)
    on_disk = M._entry_lines(
        open(M._theme_path(M._resolve("t")[1], d), encoding="utf-8").read())
    assert "cannot reach the DB" in "\n".join(on_disk), \
        "the one distinct entry was evicted by repeats, and no read-time filter can bring it back"


# ── themes that share an opening ──────────────────────────────────────────────────────────

SWE = ("You are fixing a real bug in the open-source project **%s** (language: %s).\n"
       "The repository is checked out locally at:\n  C:/x/%s")


def test_two_repositories_do_not_share_one_memory_file():
    """MEASURED ON THE LIVE STORE. Every SWE-bench goal opens with the same sentence and the
    repository name falls past the 48-character slug cap, so ansible and NodeBB produced
    different theme TITLES and the same slug -- one file, holding both. A NodeBB worker was
    primed with ansible history and the other way round; the ansible theme file on disk
    contains NodeBB entries, which is how this was found.
    """
    a = SWE % ("ansible/ansible", "python", "p05")
    b = SWE % ("NodeBB/NodeBB", "js", "p01")
    ta, tb = M.theme_from_goal(a), M.theme_from_goal(b)
    assert ta != tb, "the titles were already distinct; only the slug collapsed"
    assert M._resolve(ta, a)[1] != M._resolve(tb, b)[1]


def test_a_short_theme_keeps_its_readable_filename():
    """Themes at or under the cap are untouched, so the ordinary case stays legible and every
    existing file keeps working."""
    assert M.slugify("ansible の executor") == "ansible-の-executor"


def test_slugs_stay_within_the_cap():
    long = "x" * 300
    assert len(M.slugify(long)) <= M._SLUG_MAX


def test_the_same_theme_always_gets_the_same_slug():
    long = SWE % ("ansible/ansible", "python", "p05")
    assert M.slugify(long) == M.slugify(long)


def test_themes_differing_only_past_the_cap_still_differ():
    base = "a" * 60
    assert M.slugify(base + "one") != M.slugify(base + "two")


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


def test_the_discovery_tail_is_gone_because_discovery_never_happened():
    """THIS TEST USED TO ASSERT THE OPPOSITE, and the reason it did was never measured.

    The tail existed on the argument that a worker might DISCOVER a neighbouring theme, and a
    filter showing only what already looks related can never surface anything new. Sound in
    principle. Measured 2026-09-04 against .fleet/tool_events.jsonl: 22,444 recorded tool calls,
    and `.fleet/memory` appears in ZERO of them. Not rarely -- never. No worker has ever opened
    a theme this index offered it.

    So the tail was only ever cost, and the cost was real: a worker surveying cinemas was handed
    arithmetic one-shots and a furigana task, eight mentions of an unrelated subject in a
    3,646-character prompt.

    RELATED themes are still kept in full. Only the unrelated filler is gone, so the day the
    recall path is wired to something that reads it, raising _INDEX_RECENT_TAIL restores this
    behaviour and this test is the place that says why it was turned off."""
    lines = idx("ansible の executor", "まったく別の作業A", "まったく別の作業B")
    got = M.prune_index_lines(lines, "ansible", "fixing ansible")
    assert len(got) == 1, "an unrelated theme was primed into the goal again"
    assert "ansible" in got[0], "the related theme must still survive"


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
    # With the tail at zero this now says: twenty unrelated one-shot questions reach the
    # worker's prompt as nothing at all, which is the whole point.
    assert got == [], "unrelated one-shot themes are still being primed"


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


# ── what counts as "related" ───────────────────────────────────────────────────────────────
#
# Measured 2026-09-04 against the live store: a goal was matched against 40 themes and the
# overlap counts were 21, 5, then 1,1,1,1, then zero for the other 34. The four entries sitting
# on a single shared token were riding on する and この -- function words in almost any Japanese
# sentence -- and that is how a furigana task and two repository themes were primed into a
# cinema survey.
#
# Fixing it by requiring two tokens then broke the opposite case: "fixing ansible" shares
# exactly ONE token with an ansible theme, because that token is a whole word. _tokens does not
# produce comparable units -- Latin runs are words, CJK runs are exploded into bigrams -- so a
# count alone cannot decide this. Both failures happened within one afternoon.

def test_a_single_japanese_function_word_is_not_a_shared_topic():
    """THE MEASURED FALSE POSITIVE. する is what made furigana 'related' to cinemas."""
    lines = idx("社員名簿のフリガナを確定する作業")
    got = M.prune_index_lines(lines, "", "劇場ごとの配布状況を調査する")
    assert got == [], "a function-word bigram still counts as a shared topic"


def test_a_single_whole_word_IS_a_shared_topic():
    """THE REGRESSION THE COUNT INTRODUCED. One token, but the token names the subject."""
    lines = idx("ansible の executor")
    got = M.prune_index_lines(lines, "ansible", "fixing ansible")
    assert len(got) == 1, "an obvious match was dropped for having only one shared token"


def test_two_japanese_fragments_are_enough():
    """Fragments mean something in numbers; this is the same cinema theme spelled loosely."""
    lines = idx("劇場版まどか☆マギカの配布状況")
    got = M.prune_index_lines(lines, "", "劇場版まどか☆マギカ 配布状況を調べて")
    assert len(got) == 1


def test_shares_a_topic_reads_the_token_not_the_count():
    assert M.shares_a_topic({"ansible"}, {"ansible", "x"})            # one word: enough
    assert not M.shares_a_topic({"する"}, {"する", "作業"})            # one fragment: not
    assert M.shares_a_topic({"劇場", "配布"}, {"劇場", "配布"})        # two fragments: enough


def test_boilerplate_shared_by_most_of_the_store_is_discounted_when_ranking():
    """Every SWE-bench theme opens with the same sentence, so 'fixing' and 'project' say
    nothing about which one to open. Discounting is for RANKING only -- it must not decide
    relatedness, because in a store dominated by one topic the topic word is common too, and
    discounting it there dropped the obvious match entirely."""
    lines = idx(*["You are fixing a real bug in the open-source project **r%d**" % i
                  for i in range(12)])
    dull = M.uninformative_tokens(lines)
    assert "fixing" in dull and "project" in dull


def test_a_small_store_discounts_nothing():
    """With a handful of entries there is no distribution to read."""
    assert M.uninformative_tokens(idx("a", "b")) == set()
