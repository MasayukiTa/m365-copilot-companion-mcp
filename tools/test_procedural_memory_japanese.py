# -*- coding: utf-8 -*-
"""A memory you have to already know the answer to query is not a memory.

MEASURED 2026-08-31 against the live store, which held 41 KB of imported DB survey notes:

    "材料 トレース PAP"                        -> 10 matches
    "PAPを追うときどのテーブルを見ればいい"        ->  0
    "ワニスの配合材料はどこ"                     ->  0
    "材料の保証期限を調べたい"                    ->  0
    ... 8 of 8 realistic paraphrases            ->  0

The knowledge was stored and unreachable by the way anybody actually asks. The cause was one
line -- `query.split()` -- and Japanese does not put spaces between words, so a real question
arrived as a single unmatchable token. Only a caller who already knew to type space-separated
keywords could reach anything.

The fix used the segmenter that ALREADY EXISTED for this, in tools/data_discovery.py. Two
tokenisers would drift apart; the point of this file is that the shared one stays shared.
"""
import re

import pytest

from tools import procedural_memory as PM


def hits(text):
    m = re.match(r"(\d+) match", text or "")
    return int(m.group(1)) if m else 0


# ── tokenisation ──────────────────────────────────────────────────────────────────────────

def test_an_unsegmented_question_becomes_several_tokens():
    """The defect in one assertion: this used to be a single token."""
    toks = PM._query_tokens("PAPを追うときどのテーブルを見ればいい")
    assert len(toks) > 1
    assert any("テーブル" in t for t in toks)


def test_particles_are_cut():
    toks = PM._query_tokens("材料トレースで見るべきテーブルは")
    assert any(t == "材料トレース" or "材料トレース" in t for t in toks)


def test_a_negation_the_particle_list_cannot_cut_still_yields_its_content_runs():
    """The particle list handles particles, not negation: "使ってはいけない古いビュー" splits at
    は and leaves "いけない古いビュー" glued, so "ビュー" -- which the store holds -- never
    matched. The katakana/kanji runs inside a token are added for exactly this."""
    toks = PM._query_tokens("使ってはいけない古いビュー")
    assert "ビュー" in toks


def test_space_separated_keywords_still_work():
    """The behaviour that already worked must not be traded away for the one being added."""
    toks = PM._query_tokens("材料 トレース PAP")
    for expected in ("材料", "トレース", "pap"):
        assert expected in toks


def test_an_ascii_query_needs_no_segmentation():
    toks = PM._query_tokens("odbc query timeout")
    assert "odbc" in toks and "timeout" in toks


def test_an_empty_query_yields_nothing():
    assert PM._query_tokens("") == []
    assert PM._query_tokens("   ") == []


def test_the_shared_segmenter_is_the_one_being_used():
    """If data_discovery's segmenter is ever changed or moved, this file's premise is gone --
    and having two tokenisers is how they drift apart."""
    from tools.data_discovery import extract_keywords
    assert extract_keywords("材料トレースで見るべきテーブルは")


def test_it_never_raises_even_if_the_segmenter_is_unavailable(monkeypatch):
    """The search sits behind a tool call. Losing the segmenter must degrade to whitespace,
    not fail."""
    import builtins
    real = builtins.__import__

    def _boom(name, *a, **k):
        if name == "tools.data_discovery":
            raise ImportError("gone")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert PM._query_tokens("材料 トレース") == ["材料", "トレース"]


# ── against the real store, when there is one ─────────────────────────────────────────────

PARAPHRASES = [
    "PAPを追うときどのテーブルを見ればいい",
    "ワニスの配合材料はどこ",
    "材料の保証期限を調べたい",
    "塗工の性能測定データ",
    "製品検査の正規ビューは",
    "ロット番号から材料をたどりたい",
]


def _store_has_content():
    return hits(PM.procedural_memory_search("材料 トレース") or "") > 0


@pytest.mark.parametrize("question", PARAPHRASES)
def test_a_real_question_reaches_the_stored_knowledge(question):
    """Every one of these returned 0 before the fix."""
    if not _store_has_content():
        pytest.skip("no imported DB notes in this checkout")
    assert hits(PM.procedural_memory_search(question)) > 0, question


@pytest.mark.parametrize("question", ["今日の天気は", "会議の議事録を書いて", "犬の飼い方"])
def test_an_unrelated_question_still_matches_nothing(question):
    """THE GUARD THAT MATTERS. Widening recall by splitting more aggressively is only an
    improvement if it does not make everything match everything -- a memory that answers every
    question has stopped being a memory again, in the other direction."""
    if not _store_has_content():
        pytest.skip("no imported DB notes in this checkout")
    assert hits(PM.procedural_memory_search(question)) == 0, question
