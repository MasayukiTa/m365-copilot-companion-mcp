"""The matcher must reach the right procedure, and must never reach the wrong one.

A Japanese question carrying a date could not reach a Skill that plainly applied. "search my
mail" matched at 0.625; the same question with a month in it scored 0.375 and was refused.
Adding a date LOWERED the score, because a query token matching nothing still enlarged the
denominator -- so it counted as evidence against.

Two consultants agreed on that cause, disagreed on the cure, and each named the other's
weakness correctly. Both were measured before either was adopted:

    baseline                    13/16   3 misses   0 wrong
    denominator change alone    12/16   0 misses   4 WRONG
    tokeniser change alone      13/16   3 misses   0 wrong
    both, with a 3-token floor  16/16   0 misses   0 wrong

Half the cases below are negatives, and that is the point: removing dilution raises every
score, so a query that used to fall safely between two Skills starts landing on one. A wrong
match is the expensive failure -- the agent follows a procedure written for something else and
presents it as the user's own.

THE SKILLS HERE ARE FIXTURES, not the installed library. The first version of this file asked
the real store, which passed on my machine and failed every case on CI: `skills/` is
gitignored, so the runner has no Skills at all and every match came back None. A test that
depends on a private library is a test that only runs where that library happens to be.
"""
import pytest

from relay.skills import SkillStore, _match_tokens

MAIL = """---
name: mail-lookup
description: "メール・予定表を調べるときの手順。期間の区切り方と取りこぼしの潰し方"
when_to_use: "受信メール 送信済みメール メール検索 メール一覧 メールを調べる 予定表 打合せ の調査。期間を指定したメールの一覧化、差出人や件名での絞り込み"
---

# 手順
範囲を区切って取得する。
"""

CHART = """---
name: lot-survey
description: "資材のロットが保証期限を超えて使われていないかを調べ、散布図にする"
when_to_use: "ロット 保証期限 期限切れ 超過 の調査。資材略称を渡して使用実績を洗い出し、特性ごとの散布図を出す"
---

# 手順
洗い出して散布図にする。
"""

REPORT = """---
name: inspection-report
description: "検査結果から報告書を組み立てる"
when_to_use: "検査 報告書 の作成。測定値をまとめて所定の様式に流し込む"
---

# 手順
様式に流し込む。
"""


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """A library of three Skills, all trusted, with nothing of the real one in it."""
    tmp = tmp_path_factory.mktemp("skillmatch")
    root = tmp / "proj"
    for body in (MAIL, CHART, REPORT):
        name = [l for l in body.splitlines() if l.startswith("name:")][0].split(":", 1)[1].strip()
        d = root / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    s = SkillStore(root, db_path=tmp / "skills.sqlite3", gate_dir=tmp / "gates")
    for skill in s.discover():
        review = s.request_approval(skill.name)
        s.confirm_approval(skill.name, review["token"])
    assert all(x.trust == "trusted" for x in s.discover())
    return s


MUST_MATCH = [
    ("メールを検索したい", "mail-lookup"),
    ("受信メールの一覧を作って", "mail-lookup"),
    ("1月から3月の受信メールを調べて", "mail-lookup"),
    ("送信済みメールを日付順に一覧化して", "mail-lookup"),
    ("2026年1月のメールを検索して一覧にしたい", "mail-lookup"),
    ("先月のメールを一覧にして", "mail-lookup"),
    ("4月の送信済みメールを宛先付きで一覧にしてください", "mail-lookup"),
    ("保証期限を超過したロットを調べたい", "lot-survey"),
]

MUST_NOT_MATCH = [
    "この関数をリファクタリングして",
    "PowerPointの色を変えたい",
    "2026年の売上を集計して",
    "会議室を予約して",
]


@pytest.mark.parametrize("query,expected", MUST_MATCH)
def test_the_right_procedure_is_found(store, query, expected):
    got = store.match(query)
    assert got is not None, "%r found nothing" % query
    assert got["name"] == expected, "%r -> /%s" % (query, got["name"])


@pytest.mark.parametrize("query", MUST_NOT_MATCH)
def test_an_unrelated_question_finds_nothing(store, query):
    got = store.match(query)
    assert got is None, "%r wrongly matched /%s" % (query, got and got["name"])


def test_a_shared_word_is_not_enough(store):
    """Every fail-open measured rested on ONE shared word: a question about a mail SERVER
    landing on mail lookup, one about lot PRICES landing on the lot survey. Two bigrams of
    one word are not two pieces of evidence."""
    for query in ("メールサーバーの障害原因を調べて", "ロットの価格推移をグラフにして"):
        got = store.match(query)
        assert got is None, "%r wrongly matched /%s" % (query, got and got["name"])


def test_a_date_no_longer_costs_the_match(store):
    """The measurement that started this: 0.625 without a date, 0.375 with one."""
    plain = store.match("メールを検索したい")
    dated = store.match("2026年1月のメールを検索して一覧にしたい")
    assert plain and dated
    assert dated["name"] == plain["name"]


# ---- the tokeniser's own behaviour ---------------------------------------------------------

def test_a_boundary_straddling_bigram_does_not_become_a_token():
    """月の, を検, ルを -- one content character glued to a particle. Cutting the run on
    script boundaries removes these without anyone having to list them."""
    toks = _match_tokens("2026年1月のメールを検索して一覧にしたい")
    assert not any(t in ("月の", "を検", "ルを", "して") for t in toks), sorted(toks)


def test_the_segmentation_is_partial_and_that_is_survivable():
    """It does NOT remove every grammatical token: にしたい is a four-character hiragana run
    that slips past the short-piece rule, yielding にし/した/たい. That open-endedness is what
    one consultant warned a stop-list would always have -- and it costs nothing here, because
    those tokens appear in no Skill's metadata and the denominator drops them anyway.

    Pinned so a future tightening is a deliberate act, and so nobody reads the segmentation
    as complete."""
    toks = _match_tokens("2026年1月のメールを検索して一覧にしたい")
    assert {t for t in toks if t in ("にし", "した", "たい")}, \
        "if these are gone the tokeniser changed -- re-run scripts/win/skill_match_bench.py"


def test_content_words_survive_intact():
    toks = _match_tokens("送信済みメールを日付順に一覧化して")
    assert "メー" in toks and "ール" in toks
    assert "送信" in toks, "the kanji stem of 送信済み must survive"


def test_english_is_untouched():
    assert "powerpoint" in _match_tokens("PowerPointの色を変えたい")
