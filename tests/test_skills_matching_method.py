# -*- coding: utf-8 -*-
"""The Skill matcher's METHOD, tested piece by piece (relay/skills.py, the block above
_match_units): normalisation, n-gram features, the evidence unit, IDF, coverage, the three
guards of MatchPolicy, and the optional `keywords:` field.

tests/test_skills_business_matching.py measures how well the whole thing does on office
requests; this file pins what each part is FOR, with synthetic libraries small enough that
every number below can be worked out by hand. The guard tests call _decide() directly on
hand-made scores, so removing any one guard fails a test here whatever the corpus says.
"""
from __future__ import annotations

import time

import pytest

import relay.skills as S
from relay.skills import MatchPolicy, SkillError, SkillStore, _Scored, _decide


def feats(text):
    return set(S._match_features(text))


def lib(**docs):
    return [(name, frozenset(feats(text))) for name, text in docs.items()]


def top(text, library):
    scored = S._score_library(text, library)
    return scored[0] if scored else None


# ------------------------------------------------------------------------ normalisation

def test_full_width_and_half_width_forms_normalise_to_the_ordinary_ones():
    assert feats("ＰＣ") == feats("PC") == {"w:pc"}
    assert feats("ｶﾀｶﾅ") == feats("カタカナ")
    assert feats("１２３４") == set(), "a bare number is not a topic"


def test_katakana_and_hiragana_spellings_of_one_word_share_features():
    assert feats("トナー") == feats("となー")
    assert feats("トナー") & feats("トナーを注文")


def test_one_kana_between_kanji_is_bridged_as_okurigana():
    """打ち合わせ and 打合せ are one word spelled two ways; without the bridge 打ち合わせ is
    single characters and contributes nothing."""
    assert "c:打合" in feats("打ち合わせ")
    assert "c:打合" in feats("打合せ")
    assert "c:売上" in feats("売り上げ")


def test_a_case_particle_is_never_bridged():
    assert "c:議議" not in feats("会議の議事録")
    assert "c:費精" not in feats("経費を精算")


def test_english_stop_words_are_dropped_and_suffixes_folded():
    assert feats("the of how what please") == set()
    assert feats("booking") == feats("book")
    assert feats("visitor") == feats("visiting")
    assert feats("invoices") == feats("invoice")


def test_a_skill_name_contributes_its_words():
    skill = S.Skill("meeting-room-booking", "会議室の予約", None, "project", "d", {}, "b",
                    (), (), 0)
    assert {"w:meet", "w:room", "w:book"} <= S._skill_features(skill)


# ------------------------------------------------------------------ the evidence unit

def test_evidence_grows_with_the_length_of_what_is_shared():
    library = lib(a="経費精算の手順", b="出張申請の手順", c="会議室の予約")
    assert top("経費", library).evidence == pytest.approx(2.0)
    assert top("経費精算", library).evidence == pytest.approx(4.0)


def test_a_loanword_is_one_word_however_many_characters_it_has():
    library = lib(a="プロジェクターの手配", b="ロットの調査", c="会議室の予約")
    assert top("プロジェクター", library).evidence == pytest.approx(S._LATIN_WEIGHT)
    assert top("ロット", library).evidence == pytest.approx(S._LATIN_WEIGHT)


def test_an_english_word_is_worth_one_two_kanji_word():
    library = lib(a="expense 経費", b="trip 出張", c="room 会議室")
    assert top("expense", library).evidence == pytest.approx(top("経費", library).evidence)


def test_a_feature_every_skill_has_is_worth_less_than_a_distinctive_one():
    library = lib(a="手順 経費", b="手順 出張", c="手順 会議")
    shared = top("手順", library)
    unique = top("経費", library)
    assert shared.evidence < unique.evidence
    assert shared.distinct == 0 and unique.distinct == pytest.approx(2.0)


def test_hiragana_never_counts_against_a_skill():
    library = lib(a="経費精算の手順", b="出張申請の手順")
    plain = top("経費精算", library).coverage
    chatty = top("経費精算のやり方をおしえてくださいね", library).coverage
    assert plain == chatty == pytest.approx(1.0)


def test_unknown_content_lowers_coverage_and_known_content_elsewhere_lowers_it_more():
    library = lib(a="経費精算", b="出張申請")
    alone = top("経費精算", library).coverage
    unknown = top("経費精算と天気予報", library).coverage
    elsewhere = top("経費精算と出張申請", library).coverage
    assert alone == pytest.approx(1.0)
    assert elsewhere < unknown < alone


def test_a_kana_only_request_can_match_a_kana_keyword():
    library = lib(a="経費精算 けいひせいさん", b="出張申請 しゅっちょう")
    got = top("けいひせいさん", library)
    assert got.name == "a" and got.coverage == pytest.approx(1.0)


# ------------------------------------------------------------------------ the guards

POLICY = MatchPolicy(min_evidence=2.5, min_coverage=0.5, max_runner_up_ratio=2 / 3,
                     min_distinct=2.0)


def test_a_clear_winner_is_taken():
    best = _Scored("a", evidence=4.0, coverage=1.0, distinct=4.0)
    assert _decide([best, _Scored("b", 1.0, 0.25, 1.0)], POLICY) is best


def test_minimum_evidence_refuses_a_single_word():
    """One distinctive two-kanji word (2.0) never selects a Skill on its own."""
    assert _decide([_Scored("a", evidence=2.0, coverage=1.0, distinct=2.0)], POLICY) is None
    assert _decide([_Scored("a", evidence=2.5, coverage=1.0, distinct=2.5)], POLICY)


def test_minimum_distinct_refuses_evidence_other_skills_share():
    assert _decide([_Scored("a", evidence=5.0, coverage=1.0, distinct=1.0)], POLICY) is None
    assert _decide([_Scored("a", evidence=5.0, coverage=1.0, distinct=2.0)], POLICY)


def test_minimum_coverage_refuses_one_word_inside_a_request_about_something_else():
    assert _decide([_Scored("a", evidence=4.0, coverage=0.49, distinct=4.0)], POLICY) is None
    assert _decide([_Scored("a", evidence=4.0, coverage=0.5, distinct=4.0)], POLICY)


def test_the_margin_refuses_a_request_between_two_skills():
    best = _Scored("a", evidence=3.0, coverage=0.9, distinct=3.0)
    assert _decide([best, _Scored("b", 2.1, 0.6, 2.1)], POLICY) is None      # 0.7 > 2/3
    assert _decide([best, _Scored("b", 3.0, 0.9, 3.0)], POLICY) is None      # a tie
    assert _decide([best, _Scored("b", 1.9, 0.6, 1.9)], POLICY) is best      # 0.63 < 2/3


def test_the_trusted_policy_is_stricter_than_the_suggestion_policy():
    t, s = S._TRUSTED_POLICY, S._SUGGEST_POLICY
    assert t.min_evidence >= s.min_evidence and t.min_distinct >= s.min_distinct
    assert t.min_coverage >= s.min_coverage
    assert t.max_runner_up_ratio <= s.max_runner_up_ratio
    # The trusted door keeps the old rule's intent: no single word selects a Skill.
    assert t.min_evidence > 2.0 and t.min_distinct >= 2.0


# ------------------------------------------------------------------- through the store

def _store(tmp_path, monkeypatch, bundles, trust=True):
    monkeypatch.delenv("MCP_SKILLS_INCLUDE_PERSONAL", raising=False)
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(tmp_path / "s.sqlite3"))
    monkeypatch.setenv("MCP_SKILLS_GATE_DIR", str(tmp_path / "gates"))
    for name, front in bundles.items():
        d = tmp_path / "proj" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: %s\n%s---\n\n# %s\n\n1. do it\n"
                                    % (name, front, name), encoding="utf-8")
    store = SkillStore(tmp_path / "proj", db_path=tmp_path / "s.sqlite3",
                       gate_dir=tmp_path / "gates")
    if trust:
        for skill in store.discover():
            store.confirm_approval(skill.name, store.request_approval(skill.name)["token"])
    return store


JA = {
    "expense-claim": 'description: "経費精算の手順。領収書を添付して上長承認を得る"\n',
    "trip-request": 'description: "出張申請の手順。旅費の概算と事前承認"\n',
    "room-booking": 'description: "会議室の予約手順。人数と設備で部屋を選ぶ"\n',
}


def test_a_single_compound_word_matches(tmp_path, monkeypatch):
    """The old rule demanded two separate words; 経費精算 alone is four characters of
    distinctive evidence."""
    store = _store(tmp_path, monkeypatch, JA)
    assert store.match("経費精算のやり方を教えて")["name"] == "expense-claim"


def test_a_single_short_word_does_not(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, JA)
    assert store.match("経費の予算を立てたい") is None


def test_two_skills_equally_named_by_a_request_match_neither(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, {
        "a-procedure": 'description: "設備点検と在庫棚卸"\n',
        "b-procedure": 'description: "設備点検と在庫棚卸の記録"\n',
        "c-procedure": 'description: "会議室の予約"\n',
    })
    assert store.match("設備点検と在庫棚卸") is None


def test_an_explicit_name_is_taken(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, JA)
    assert store.match("/room-booking で頼む")["name"] == "room-booking"


def test_keywords_let_an_english_request_reach_a_japanese_skill(tmp_path, monkeypatch):
    front = dict(JA)
    base = _store(tmp_path / "without", monkeypatch, front)
    request = "How do I file a reimbursement for receipts?"
    assert base.match(request) is None
    front["expense-claim"] += "keywords: [reimbursement, receipt, けいひせいさん]\n"
    with_kw = _store(tmp_path / "with", monkeypatch, front)
    assert with_kw.match(request)["name"] == "expense-claim"
    assert with_kw.match("けいひせいさん の しかた")["name"] == "expense-claim"


def test_keywords_are_matching_text_for_unapproved_skills_too(tmp_path, monkeypatch):
    front = dict(JA)
    front["expense-claim"] += "keywords: [reimbursement, receipt]\n"
    store = _store(tmp_path, monkeypatch, front, trust=False)
    assert store.match("reimbursement for my receipts") is None
    assert store.match_unapproved("reimbursement for my receipts")["name"] == "expense-claim"


# ---------------------------------------------------------------- keywords validation

@pytest.mark.parametrize("value,message", [
    ("expense, 経費", "YAML list"),
    ({"a": 1}, "YAML list"),
    ([1, 2], "non-empty string"),
    (["ok", ""], "non-empty string"),
    (["ok", "   "], "non-empty string"),
    (["x"] * (S.MAX_KEYWORDS + 1), "more than"),
    (["x" * (S.MAX_KEYWORD_CHARS + 1)], "exceeds"),
    (["x" * 90] * 12, "in total"),
])
def test_malformed_keywords_are_refused(value, message):
    with pytest.raises(SkillError, match=message):
        S._validate_keywords(value)


def test_absent_or_well_formed_keywords_are_accepted():
    assert S._validate_keywords(None) == []
    assert S._validate_keywords([" expense ", "経費"]) == ["expense", "経費"]


def test_a_bundle_with_malformed_keywords_is_listed_as_invalid(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, {
        "bad-keywords": 'description: "経費精算"\nkeywords: expense, 経費\n'}, trust=False)
    assert store.discover() == []
    assert "keywords" in store.invalid_bundles()["bad-keywords"]


# ------------------------------------------------------------------------------ latency

def test_scoring_500_skills_is_well_under_a_second():
    """The feature sets are memoised by content (and filled when the bundle cache loads a
    bundle); what a match pays per request is one pass over the library."""
    skills = [S.Skill("proc-%03d" % i, "部署%d: 台帳%dの更新手順と確認方法" % (i % 12, i),
                      None, "project", "d%d" % i, {}, "b", (), (), 0) for i in range(500)]
    library = [(s.name, S._skill_features(s)) for s in skills]
    t0 = time.perf_counter()
    for _ in range(5):
        S._score_library("台帳123の更新手順を確認したい", library)
    per_match = (time.perf_counter() - t0) / 5
    assert per_match < 0.25, per_match
