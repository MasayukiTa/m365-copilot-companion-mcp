# -*- coding: utf-8 -*-
"""How well does Skill matching work for ordinary office requests? A MEASUREMENT, not a tuning set.

The corpus is 18 Skills a Japanese office would actually keep (tests/fixtures/skills_business).
The requests below were written BEFORE the matcher was run against them, in the registers people
really use: polite and casual Japanese, kana-for-kanji and wrong-kanji typos, English, requests
that must match nothing (unrelated, and near misses that share a word with a Skill), and
ambiguous ones where two Skills or none are all acceptable.

NOTHING IN relay/skills.py WAS CHANGED TO MAKE THESE PASS, and nothing may be: this repository
forbids fitting a matcher to its own benchmark. The floors asserted at the bottom are set BELOW
what the current implementation measured, so the test guards against regression without
claiming more than was observed. The measured numbers and every miss are printed (run with -s).

Metrics:
  recall     = correct top-1 / requests that have one right Skill
  precision  = correct / every match returned (wrong Skill, or any Skill on a match-nothing
               request, counts against it)
  FP rate    = matches returned on the match-nothing set / size of that set
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from relay.skills import SkillStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skills_business"
BUSINESS = sorted(p.name for p in FIXTURES.iterdir() if (p / "SKILL.md").is_file())

EXP = "expense-reimbursement"
MIN = "meeting-minutes-summary"
SAL = "monthly-sales-excel"
CMP = "customer-complaint-first-response"
TRP = "business-trip-request"
INV = "invoice-check"
WKR = "weekly-report-template"
ROOM = "meeting-room-booking"
ONB = "new-hire-onboarding"
RNG = "ringi-document"
STK = "inventory-stocktake"
D8 = "quality-defect-8d-report"
ENG = "english-email-reply"
CTR = "contract-review-points"
INC = "incident-report"
PTO = "paid-leave-request"
PUR = "purchase-request"
VIS = "visitor-reception"

# (request, expected, kind). expected: a Skill name, None (must match nothing), or a tuple of
# acceptable outcomes for an ambiguous request (None inside the tuple = declining is fine).
LABELLED = [
    # --- expense-reimbursement
    ("先月の交通費を精算したいんですが、どうすればいいですか", EXP, "ja-polite"),
    ("経費精算のやり方教えて", EXP, "ja-casual"),
    ("領収書をなくしたけど立替えたお金を返してもらいたい", EXP, "ja-paraphrase"),
    ("接待で使ったお金の経費申請ってどうやるの", EXP, "ja-casual"),
    ("経費清算の手順を教えてください", EXP, "ja-typo"),
    ("How do I submit an expense claim for my taxi receipts?", EXP, "en"),
    # --- meeting-minutes-summary
    ("さっきの会議の議事録をまとめてください", MIN, "ja-polite"),
    ("打ち合わせのメモから決定事項とToDoを抜き出して", MIN, "ja-paraphrase"),
    ("文字起こしを議事録っぽく短くして", MIN, "ja-casual"),
    ("ぎじろくを要約して", MIN, "ja-typo"),
    ("Summarize the meeting notes into minutes with action items", MIN, "en"),
    # --- monthly-sales-excel
    ("今月の売上をエクセルで集計したい", SAL, "ja-paraphrase"),
    ("得意先ごとの売上を前年と比べる表を作って", SAL, "ja-casual"),
    ("営業会議用に月次の売上実績をまとめてください", SAL, "ja-polite"),
    ("月次売り上げの集計お願い", SAL, "ja-typo"),
    ("Build a monthly sales pivot table in Excel", SAL, "en"),
    # --- customer-complaint-first-response
    ("お客様からクレームの電話があった。最初にどう対応すればいい？", CMP, "ja-casual"),
    ("怒っている顧客へのお詫びメールの書き方", CMP, "ja-paraphrase"),
    ("苦情を受けたときの初動対応を知りたいです", CMP, "ja-polite"),
    ("クレーム対おうの手順", CMP, "ja-typo"),
    ("A customer is angry about a late delivery, how should I respond first?", CMP, "en"),
    # --- business-trip-request
    ("来週大阪へ出張するので申請したい", TRP, "ja-paraphrase"),
    ("海外出張の手続きを教えてください", TRP, "ja-polite"),
    ("新幹線とホテルを取る前に何が必要？出張の承認とか", TRP, "ja-casual"),
    ("しゅっちょう申請のやり方", TRP, "ja-typo"),
    ("I need to request approval for a business trip to Singapore", TRP, "en"),
    # --- invoice-check
    ("取引先から来た請求書を確認してほしい", INV, "ja-casual"),
    ("請求書の金額が発注書と合っているかチェックしたい", INV, "ja-paraphrase"),
    ("インボイスの登録番号の確認方法を教えてください", INV, "ja-polite"),
    ("せいきゅうしょのチェック", INV, "ja-typo"),
    ("Please check this vendor invoice against the purchase order", INV, "en"),
    # --- weekly-report-template
    ("週報を書かなきゃ。テンプレートある？", WKR, "ja-casual"),
    ("今週の実績と来週の予定をまとめた報告を作りたい", WKR, "ja-paraphrase"),
    ("周報のテンプレ", WKR, "ja-typo"),
    ("Write my weekly status report", WKR, "en"),
    # --- meeting-room-booking
    ("明日の14時から会議室を予約したい", ROOM, "ja-paraphrase"),
    ("10人入れてプロジェクターがある部屋を取って", ROOM, "ja-casual"),
    ("応接室の予約をキャンセルしたいのですが", ROOM, "ja-polite"),
    ("Book a meeting room for 6 people tomorrow afternoon", ROOM, "en"),
    # --- new-hire-onboarding
    ("来月新人が配属されるので受け入れ準備をしたい", ONB, "ja-paraphrase"),
    ("中途入社の人の初日に何を用意すればいい？", ONB, "ja-casual"),
    ("新入社員のOJT計画を立てたいと考えております", ONB, "ja-polite"),
    ("What do we need to prepare for a new hire's first day?", ONB, "en"),
    # --- ringi-document
    ("稟議書の書き方を教えて", RNG, "ja-casual"),
    ("新しい設備を買うための社内決裁を取りたい", RNG, "ja-paraphrase"),
    ("起案の文章を決裁者に通りやすくしたいです", RNG, "ja-polite"),
    ("How to write an internal approval request (ringi) for a new software license", RNG, "en"),
    # --- inventory-stocktake
    ("月末の棚卸をやるので手順を知りたい", STK, "ja-paraphrase"),
    ("帳簿と実際の在庫数が合わない。差異をどう処理する？", STK, "ja-casual"),
    ("たなおろしの準備", STK, "ja-typo"),
    ("How do we do the year-end inventory count?", STK, "en"),
    # --- quality-defect-8d-report
    ("品質不良の報告書を8Dで書きたい", D8, "ja-paraphrase"),
    ("お客様から是正処置報告書を求められました", D8, "ja-polite"),
    ("不良品が流出した。なぜなぜ分析から恒久対策までまとめたい", D8, "ja-casual"),
    ("Prepare an 8D corrective action report for the defective parts", D8, "en"),
    # --- english-email-reply
    ("海外の取引先に英語でメールを返したい", ENG, "ja-paraphrase"),
    ("英文メールの返信を手伝って。納期の問い合わせへの回答", ENG, "ja-casual"),
    ("英分メールの返信", ENG, "ja-typo"),
    ("Reply to this email from our US supplier politely", ENG, "en"),
    # --- contract-review-points
    ("NDAを結ぶ前にチェックすべき点は？", CTR, "ja-casual"),
    ("業務委託契約書のレビューをお願いしたいです", CTR, "ja-polite"),
    ("契約書の損害賠償の条項が大丈夫か見てほしい", CTR, "ja-paraphrase"),
    ("Review this NDA before we sign it", CTR, "en"),
    # --- incident-report
    ("昨日のサーバ停止の障害報告書を書きたい", INC, "ja-paraphrase"),
    ("システムが落ちた件の報告をまとめて。再発防止策も", INC, "ja-casual"),
    ("ネットワーク障害のポストモーテムを作成してください", INC, "ja-polite"),
    ("Draft a postmortem for yesterday's outage", INC, "en"),
    # --- paid-leave-request
    ("来週金曜に有給を取りたい", PTO, "ja-casual"),
    ("午前休の申請はどうやりますか", PTO, "ja-polite"),
    ("夏休みの休暇申請", PTO, "ja-paraphrase"),
    ("How do I apply for paid leave?", PTO, "en"),
    # --- purchase-request
    ("備品を買いたいので購買依頼を出したい", PUR, "ja-paraphrase"),
    ("見積もりを取って発注するまでの流れ", PUR, "ja-casual"),
    ("トナーを注文したいのですが", PUR, "ja-polite"),
    ("I want to order a new monitor for the office", PUR, "en"),
    # --- visitor-reception
    ("明日お客様が来社されるので受付の準備をしたい", VIS, "ja-polite"),
    ("来訪者の入館証の手配", VIS, "ja-paraphrase"),
    ("A client is visiting our office tomorrow, what should reception do?", VIS, "en"),

    # --- must match NOTHING: unrelated
    ("今日の天気は？", None, "none-unrelated"),
    ("このPythonの関数をリファクタリングして", None, "none-unrelated"),
    ("PowerPointのスライドの色を変えたい", None, "none-unrelated"),
    ("おすすめのランチの店を教えて", None, "none-unrelated"),
    ("PCのパスワードを忘れた", None, "none-unrelated"),
    ("Wi-Fiにつながらない", None, "none-unrelated"),
    ("年末調整の書類の書き方", None, "none-unrelated"),
    ("健康診断の予約をしたい", None, "none-unrelated"),
    ("社内報の原稿を書いて", None, "none-unrelated"),
    ("プリンタが紙詰まりした", None, "none-unrelated"),
    ("Teamsの通知が来ない", None, "none-unrelated"),
    ("What is the capital of France?", None, "none-unrelated"),
    ("Translate this sentence into Spanish", None, "none-unrelated"),
    # --- must match NOTHING: near misses that share a word with a Skill
    ("経費の予算を来期分立てたい", None, "none-nearmiss"),
    ("会議の日程調整をしたい", None, "none-nearmiss"),
    ("来期の売上目標を立てる", None, "none-nearmiss"),
    ("請求書を発行したい", None, "none-nearmiss"),
    ("契約社員の更新手続き", None, "none-nearmiss"),
    ("出張先でおすすめの観光地", None, "none-nearmiss"),
    ("在庫があるか営業に聞かれた", None, "none-nearmiss"),
    ("新人の歓迎会の店を予約して", None, "none-nearmiss"),
    ("障害者雇用の制度について知りたい", None, "none-nearmiss"),
    ("品質管理の資格試験の勉強法", None, "none-nearmiss"),
    ("英語の勉強法を教えて", None, "none-nearmiss"),
    ("週末の予定を立てたい", None, "none-nearmiss"),

    # --- ambiguous: any listed outcome is acceptable
    ("出張の交通費を精算したい", (EXP, TRP, None), "ambiguous"),
    ("海外のお客様からのクレームに英語で返信したい", (ENG, CMP, None), "ambiguous"),
    ("顧客から品質クレームが来て報告書を出せと言われた", (D8, CMP, None), "ambiguous"),
    ("会議室で打ち合わせした内容をまとめたい", (MIN, None), "ambiguous"),
    ("購入したい設備の稟議を書く", (RNG, PUR, None), "ambiguous"),
    ("新人に経費精算のやり方を教えたい", (EXP, ONB, None), "ambiguous"),
    ("クレームの件数を月次で集計", (CMP, None), "ambiguous"),
]


def _isolated_env(mp, tmp):
    # The operator may run with the personal library opted in; this measurement must see the
    # 18 fixture Skills and nothing else.
    mp.delenv("MCP_SKILLS_INCLUDE_PERSONAL", raising=False)
    mp.setenv("MCP_SKILLS_STATE_DB", str(tmp / "state" / "skills.sqlite3"))
    mp.setenv("MCP_SKILLS_GATE_DIR", str(tmp / "gates"))


def _build(tmp, trust):
    root = tmp / "proj"
    (root / "skills").mkdir(parents=True)
    for name in BUSINESS:
        shutil.copytree(FIXTURES / name, root / "skills" / name)
    store = SkillStore(root, db_path=tmp / "state" / "skills.sqlite3", gate_dir=tmp / "gates")
    if trust:
        for skill in store.discover():
            review = store.request_approval(skill.name)
            store.confirm_approval(skill.name, review["token"])
    return store


@pytest.fixture(scope="module")
def trusted_store(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("biz_trusted")
    _isolated_env(mp, tmp)
    try:
        yield _build(tmp, trust=True)
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def untrusted_store(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("biz_untrusted")
    _isolated_env(mp, tmp)
    try:
        yield _build(tmp, trust=False)
    finally:
        mp.undo()


def _acceptable(expected):
    if isinstance(expected, tuple):
        return set(expected)
    return {expected}


def _measure(fn):
    rows = []
    for query, expected, kind in LABELLED:
        t0 = time.perf_counter()
        got = fn(query)
        dt = time.perf_counter() - t0
        rows.append((query, expected, kind, (got or {}).get("name"), (got or {}).get("score"), dt))
    single = [r for r in rows if isinstance(r[1], str)]
    none = [r for r in rows if r[1] is None]
    amb = [r for r in rows if isinstance(r[1], tuple)]
    correct = [r for r in single if r[3] == r[1]]
    wrong = [r for r in single if r[3] is not None and r[3] != r[1]]
    missed = [r for r in single if r[3] is None]
    fp = [r for r in none if r[3] is not None]
    amb_bad = [r for r in amb if r[3] not in _acceptable(r[1])]
    returned = [r for r in single + none if r[3] is not None]
    by_kind = {}
    for r in single:
        k = by_kind.setdefault(r[2], [0, 0])
        k[1] += 1
        k[0] += int(r[3] == r[1])
    return {
        "rows": rows, "n": len(rows), "n_single": len(single), "n_none": len(none),
        "n_amb": len(amb), "correct": correct, "wrong": wrong, "missed": missed, "fp": fp,
        "amb_bad": amb_bad,
        "recall": len(correct) / len(single),
        "precision": (len(correct) / len(returned)) if returned else 1.0,
        "fp_rate": len(fp) / len(none),
        "by_kind": by_kind,
        "latency_ms_max": 1000 * max(r[5] for r in rows),
        "latency_ms_mean": 1000 * sum(r[5] for r in rows) / len(rows),
    }


def _report(title, m):
    print("\n==== %s ====" % title)
    print("requests: %d (single-answer %d, match-nothing %d, ambiguous %d)"
          % (m["n"], m["n_single"], m["n_none"], m["n_amb"]))
    print("recall    %.3f  (%d/%d)" % (m["recall"], len(m["correct"]), m["n_single"]))
    print("precision %.3f" % m["precision"])
    print("FP rate on match-nothing set %.3f (%d/%d)" % (m["fp_rate"], len(m["fp"]), m["n_none"]))
    print("wrong Skill on single-answer: %d ; ambiguous outside the acceptable set: %d"
          % (len(m["wrong"]), len(m["amb_bad"])))
    print("latency per match: mean %.1f ms, max %.1f ms"
          % (m["latency_ms_mean"], m["latency_ms_max"]))
    print("recall by kind: " + ", ".join(
        "%s %d/%d" % (k, v[0], v[1]) for k, v in sorted(m["by_kind"].items())))
    for label, key in (("WRONG", "wrong"), ("FALSE POSITIVE", "fp"),
                       ("AMBIGUOUS-BAD", "amb_bad"), ("MISS", "missed")):
        for q, exp, kind, got, score, _dt in m[key]:
            print("  %-15s [%s] %r expected=%s got=%s score=%s" % (label, kind, q, exp, got, score))


def test_the_corpus_is_realistic_and_every_bundle_loads(trusted_store):
    skills = trusted_store.discover()
    assert len(skills) == len(BUSINESS) >= 15
    assert trusted_store.invalid_bundles() == {}
    assert all(s.trust == "trusted" for s in skills)
    with_resources = [s for s in skills if len(s.files) > 1]
    with_arguments = [s for s in skills if "$ARGUMENTS" in s.body or "$0" in s.body]
    assert len(with_resources) >= 5, [s.name for s in with_resources]
    assert len(with_arguments) >= 4, [s.name for s in with_arguments]


def test_the_labelled_set_is_large_and_balanced():
    assert len(LABELLED) >= 80
    kinds = {k for _q, _e, k in LABELLED}
    assert {"ja-polite", "ja-casual", "ja-typo", "en", "none-unrelated", "none-nearmiss",
            "ambiguous"} <= kinds
    named = {e for _q, e, _k in LABELLED if isinstance(e, str)}
    assert named == set(BUSINESS), "every Skill has at least one request aimed at it"
    assert sum(1 for _q, e, _k in LABELLED if e is None) >= 20


# ---------------------------------------------------------------------------------------------
# THE MEASUREMENT. Floors are the measured values rounded DOWN with a little headroom; see the
# module docstring. Measured 2026-09-24 on the unmodified matcher -- printed by _report.
# ---------------------------------------------------------------------------------------------

#: trusted-store match(). Measured: recall 0.436 (34/78), precision 1.000, FP 0/25, one
#: ambiguous request outside its acceptable set ('クレームの件数を月次で集計' -> monthly-sales-excel).
#: By kind: ja-paraphrase 12/18, ja-casual 11/18, ja-polite 9/15, ja-typo 1/9, en 1/18.
FLOOR_RECALL = 0.40
FLOOR_JAPANESE_RECALL = 0.50          # measured 33/60 = 0.55
FLOOR_PRECISION = 0.95
CEIL_FP_RATE = 0.04                   # at most 1 of 25
CEIL_WRONG = 2

#: untrusted-store match_unapproved() -- the "ask a human to approve this" path.
#: Measured: top-1 0.641 (50/78), precision 0.847, FP 7/25 = 0.28, 2 wrong Skills.
FLOOR_UNAPPROVED_TOP1 = 0.58
CEIL_UNAPPROVED_FP_RATE = 0.36        # at most 9 of 25


def test_matching_quality_on_office_requests(trusted_store):
    m = _measure(trusted_store.match)
    _report("SkillStore.match (all 18 trusted)", m)
    assert m["recall"] >= FLOOR_RECALL
    ja = [v for k, v in m["by_kind"].items() if k.startswith("ja-")]
    assert sum(v[0] for v in ja) / sum(v[1] for v in ja) >= FLOOR_JAPANESE_RECALL
    assert m["precision"] >= FLOOR_PRECISION
    assert m["fp_rate"] <= CEIL_FP_RATE
    assert len(m["wrong"]) + len(m["amb_bad"]) <= CEIL_WRONG


def test_approval_suggestion_quality_on_office_requests(untrusted_store):
    """match_unapproved decides which Skill a person is ASKED to approve. Looser by design."""
    m = _measure(untrusted_store.match_unapproved)
    _report("SkillStore.match_unapproved (all 18 untrusted)", m)
    assert m["recall"] >= FLOOR_UNAPPROVED_TOP1
    assert m["fp_rate"] <= CEIL_UNAPPROVED_FP_RATE


def test_nothing_trusted_means_nothing_matches(untrusted_store):
    for query, _expected, _kind in LABELLED:
        assert untrusted_store.match(query) is None, query
