"""The matcher must reach an approved procedure, and must never reach the wrong one.

Two consultants were asked why a Japanese question carrying a date missed a Skill that plainly
applied. They agreed on the cause -- a query token matching nothing still enlarged the
denominator, so adding a date to a question LOWERED its score -- and disagreed on the cure.
Both cures were measured on the fixture below before either was adopted:

    baseline                     13/16   3 misses   0 wrong
    denominator change alone      12/16   0 misses   4 WRONG
    tokeniser change alone        13/16   3 misses   0 wrong
    both, with a 3-token floor    16/16   0 misses   0 wrong

The four wrong matches are why the negatives here are half the set: removing dilution raises
every score, so a query that used to fall safely between two Skills starts landing on one. A
wrong match is the expensive failure -- the agent follows a procedure written for something
else and presents it as the user's own.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from relay.skills import SkillStore, _match_tokens   # noqa: E402


@pytest.fixture(scope="module")
def store():
    return SkillStore(ROOT)


MUST_MATCH = [
    ("メールを検索したい", "mail-lookup"),
    ("受信メールの一覧を作って", "mail-lookup"),
    ("1月から3月の受信メールを調べて", "mail-lookup"),
    ("送信済みメールを日付順に一覧化して", "mail-lookup"),
    ("2026年1月のメールを検索して一覧にしたい", "mail-lookup"),
    ("先月のメールを一覧にして", "mail-lookup"),
    ("4月の送信済みメールを宛先付きで一覧にしてください", "mail-lookup"),
    ("銅箔の保証期限超過ロットを調べたい", "copper-foil-survey"),
]

MUST_NOT_MATCH = [
    "この関数をリファクタリングして",
    "PowerPointの色を変えたい",
    "2026年の売上を集計して",
    "先月の請求書を作って",
    "メールサーバーの障害原因を調べて",
    "銅箔の価格推移をグラフにして",
    "会議室を予約して",
]


@pytest.mark.parametrize("query,expected", MUST_MATCH)
def test_the_right_procedure_is_found(store, query, expected):
    got = store.match(query)
    assert got is not None, "%r found nothing" % query
    assert got["name"] == expected, "%r -> /%s" % (query, got["name"])


@pytest.mark.parametrize("query", MUST_NOT_MATCH)
def test_an_unrelated_question_finds_nothing(store, query):
    """Each of these shares vocabulary with a Skill while asking for something else. Three of
    them matched wrongly when only the denominator was changed."""
    got = store.match(query)
    assert got is None, "%r wrongly matched /%s" % (query, got and got["name"])


# ---- the tokeniser's own behaviour ---------------------------------------------------------

def test_a_boundary_straddling_bigram_does_not_become_a_token():
    """月の, を検, ルを -- one content character glued to a particle. Cutting the run on
    script boundaries removes these without anyone having to list them."""
    toks = _match_tokens("2026年1月のメールを検索して一覧にしたい")
    assert not any(t in ("月の", "を検", "ルを", "して") for t in toks), sorted(toks)


def test_the_segmentation_is_partial_and_that_is_survivable():
    """It does NOT remove every grammatical token: にしたい is a four-character hiragana run
    and slips past the short-piece rule, yielding にし/した/たい. This is the open-endedness
    one consultant warned a stop-list would always have -- and the reason the fix is two
    halves. Those tokens appear in no Skill's metadata, so the library-vocabulary denominator
    drops them anyway; they cost nothing rather than costing a match.

    Pinned so that a future tightening of the tokeniser is a deliberate act, and so nobody
    reads the segmentation as complete."""
    toks = _match_tokens("2026年1月のメールを検索して一覧にしたい")
    leftovers = {t for t in toks if t in ("にし", "した", "たい")}
    assert leftovers, "if these are gone the tokeniser changed -- re-run the bench"


def test_content_words_survive_intact():
    toks = _match_tokens("送信済みメールを日付順に一覧化して")
    assert "メー" in toks and "ール" in toks
    assert "送信" in toks, "the kanji stem of 送信済み must survive"


def test_english_is_untouched():
    assert "powerpoint" in _match_tokens("PowerPointの色を変えたい")


def test_a_date_no_longer_costs_the_match(store):
    """The measurement that started this: 0.625 without a date, 0.375 with one."""
    plain = store.match("メールを検索したい")
    dated = store.match("2026年1月のメールを検索して一覧にしたい")
    assert plain and dated
    assert dated["name"] == plain["name"]
