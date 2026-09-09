# -*- coding: utf-8 -*-
"""relay/splittability.py: a pure, offline classifier -- no fixture needs the real fleet
machinery, only the function itself. Fixture goal texts here are SYNTHETIC, built to match the
real shapes discovered extracting .fleet/**/transcripts/*.jsonl(.gz) (2026-09-09, 1214 real
goal occurrences, 562 unique texts) without reproducing the real corpus's own content, which
carries employee names and internal case identifiers that must not enter this repo (see
project_history_rewrite_20260703.md's standing rule against identifying names in the repo).

Real-data evaluation itself (miss/over-split rate against the actual 562 unique goal texts)
lives outside pytest, in the scratchpad extraction+eval scripts used to design this module --
see the codex-plan item 6 commit message for the measured numbers. These tests pin the
FUNCTION'S contract so a future edit cannot silently change what it decides; they are not a
substitute for that real-data evaluation, and do not claim to be.
"""
import pytest

from relay import splittability as sp


# ---------------------------------------------------------------------------------------------
# Verdict / decision enum
# ---------------------------------------------------------------------------------------------

def test_verdict_rejects_an_unknown_decision():
    with pytest.raises(ValueError):
        sp.Verdict("MAYBE", "not one of the three")


def test_should_split_is_true_only_for_split():
    assert sp.Verdict(sp.SPLIT, "x").should_split is True
    assert sp.Verdict(sp.NO_SPLIT, "x").should_split is False
    assert sp.Verdict(sp.UNCERTAIN, "x").should_split is False


def test_empty_text_is_no_split_not_a_crash():
    for text in ("", "   ", None):
        v = sp.judge(text)
        assert v.decision == sp.NO_SPLIT


# ---------------------------------------------------------------------------------------------
# NO_SPLIT: short single lookups/computations (the plan's own "67字の実ジョブ" shape)
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "次の足し算の答えを数字だけで書いてください: 137 + 486。最後の行に DONE と書いてください。",
    "デスクトップにある.mdファイルを一覧にして、それぞれのサイズも教えて",
    "西暦2000年1月1日は何曜日でしたか。曜日だけで答えてください。",
    "relay フォルダにある Python ファイルのうち、名前に socket を含むものを一覧してください。",
])
def test_short_single_lookups_are_not_split(text):
    v = sp.judge(text)
    assert v.decision == sp.NO_SPLIT
    assert v.should_split is False


def test_a_67_char_job_is_judged_not_split_matching_the_plans_own_example():
    """The codex plan's own cited counter-example to length-as-proxy: a genuinely short real
    job (67 chars in the plan's measurement) that the OLD length-only trigger correctly left
    alone. This module must reach the same answer by an actual independence judgment, not by
    accident of being under the length threshold too."""
    text = "1から20までの整数のうち素数を、小さい順にカンマ区切りで書いてください。"
    assert len(text) < 100
    v = sp.judge(text)
    assert v.should_split is False


# ---------------------------------------------------------------------------------------------
# NO_SPLIT: already a fan-out child (must not be judged for further splitting)
# ---------------------------------------------------------------------------------------------

def test_a_fanout_child_with_its_own_range_marker_is_not_split_again():
    text = ("氏名とフリガナを1行出力する。" * 40 +
            "【この会話が担当する範囲 — 全体の 6/6】"
            "田中太郎=taro.tanaka の氏名とフリガナを出力する。"
            "上の範囲だけを担当してください。"
            "他の範囲は別の会話が並行して担当しているので、手を出さないこと。")
    assert len(text) >= 600, "must exercise the child-marker check ahead of the length fallback"
    v = sp.judge(text)
    assert v.decision == sp.NO_SPLIT
    assert v.signals.get("is_child") is True


def test_the_completed_range_marker_alone_is_enough():
    v = sp.judge("残りの担当範囲を完了したら DONE と書いてください。")
    assert v.decision == sp.NO_SPLIT
    assert v.signals.get("is_child") is True


# ---------------------------------------------------------------------------------------------
# NO_SPLIT / SPLIT: continuation goals ("前回タスクの続き") -- judge the NEW instruction, not
# the restated previous goal, so restated context cannot inflate the length signal.
# ---------------------------------------------------------------------------------------------

def test_a_continuation_with_a_short_new_instruction_is_not_split_despite_long_total_text():
    long_restated_goal = "前回の詳しい調査依頼の全文。" * 40
    text = ("【前回タスクの続き】\n前回のゴール: %s\n"
            "前回の成果物はディスク上に保存済み。まず読み直してから次を実行して。\n"
            "追加指示: 横断検索を実施して") % long_restated_goal
    assert len(text) >= 600
    v = sp.judge(text)
    assert v.decision == sp.NO_SPLIT
    assert v.signals.get("unwrapped_continuation") is True


def test_a_continuation_whose_new_instruction_is_itself_splittable_is_split():
    text = ("【前回タスクの続き】\n前回のゴール: 何かの調査。\n"
            "追加指示: 1月から4月のメールを一覧して、件名と差出人を出して")
    v = sp.judge(text)
    assert v.decision == sp.SPLIT
    assert v.signals.get("unwrapped_continuation") is True


def test_a_continuation_marker_with_no_additional_instruction_falls_back_to_full_text():
    text = "前回タスクの続き。前回の話の流れを踏まえて対応して。"
    v = sp.judge(text)
    # no "追加指示:" tail -- _unwrap_continuation must fall back rather than crash
    assert v.decision in (sp.NO_SPLIT, sp.UNCERTAIN, sp.SPLIT)


# ---------------------------------------------------------------------------------------------
# SPLIT: explicit author-specified split hints (the strongest real signal)
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "1応答が大きいため、日付を1〜2日ずつに区切って取得し、こまめに追記保存してください。",
    "この作業を分割する場合も、子タスクにこの出力形式をそのまま守らせること。",
    "全体を5件ずつに分けて処理してください。",
])
def test_explicit_split_hints_are_split(text):
    v = sp.judge(text)
    assert v.decision == sp.SPLIT


# ---------------------------------------------------------------------------------------------
# SPLIT: per-item lookup over an enumerated list of independent named targets (the theater-list
# shape -- embarrassingly parallel, no shared search context between items)
# ---------------------------------------------------------------------------------------------

def test_per_item_lookup_over_a_bracketed_catalogue_is_split():
    text = ("配布状況を、以下の劇場について1館ずつWeb検索で調べてください。"
            "【劇場あ, 劇場い, 劇場う, 劇場え, 劇場お, 劇場か】")
    v = sp.judge(text)
    assert v.decision == sp.SPLIT
    assert v.signals.get("bracketed_list_items", 0) >= 3


def test_a_bracketed_catalogue_with_slash_separators_is_also_counted():
    """A real production goal (社員名簿のフリガナ確定, 2026-08-28) used "/" as its list
    separator, not a comma -- the count must not depend on which one was used."""
    text = ("氏名とフリガナを確定する作業。【"
            "山田太郎=taro.yamada / 鈴木花子=hanako.suzuki / 佐藤次郎=jiro.sato / "
            "高橋三郎=saburo.takahashi】")
    v = sp.judge(text)
    assert v.decision == sp.SPLIT
    assert v.signals.get("bracketed_list_items", 0) >= 4


def test_a_short_bracketed_list_under_three_items_is_not_enough_alone():
    text = "以下を確認してください。【案件A, 案件B】"
    v = sp.judge(text)
    assert v.decision != sp.SPLIT


# ---------------------------------------------------------------------------------------------
# SPLIT: multi-month date range (this repo's own established mail-lookup convention)
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "1〜3月のメールを一覧化する",
    "1月から4月のメールを一覧して、件名と差出人を出して",
    "2026年1月〜4月の送信済みメールを一覧化してください。",
])
def test_multi_month_ranges_are_split(text):
    v = sp.judge(text)
    assert v.decision == sp.SPLIT


def test_a_within_month_day_range_buried_in_one_sub_question_is_not_split():
    """The real false positive this module's date-range signal was narrowed to avoid: an
    incidental day-level span naming when a single past event happened (here, a 3-day
    measurement window within one month), inside an otherwise single-case investigation --
    not a task-scoping range implying the WHOLE goal chops by period."""
    text = ("調査依頼。4月28-30日に実施した測定の結果を確認してください。"
            "(1)測定項目と測定点数 (2)集計ファイルの内容と結論 (3)報告先"
            "(4)結論の根拠。確認できない場合は確認できずと明記してください。")
    v = sp.judge(text)
    assert v.decision != sp.SPLIT


# ---------------------------------------------------------------------------------------------
# UNCERTAIN: numbered sub-questions about ONE case/thread -- measured in real production
# (campaign c7e01b58b1956, 2026-09-09) to over-split when naively fanned out (4/7 refused).
# Must default to NOT splitting, exactly like JudgeUnavailable in command_judge.py.
# ---------------------------------------------------------------------------------------------

def test_numbered_subquestions_about_one_case_are_uncertain_not_split():
    text = ("ある案件について、2026年の経緯と結果を確認してください。"
            "(1)評価の目的 (2)測定項目の結果 (3)測定結果の詳細 "
            "(4)結論と対策 (5)担当範囲。確認できない場合は確認できずと明記してください。")
    v = sp.judge(text)
    assert v.decision == sp.UNCERTAIN
    assert v.should_split is False
    assert v.signals.get("numbered_items", 0) >= 3


def test_uncertain_is_never_treated_as_split_by_a_caller_using_should_split():
    """The contract a caller must rely on: decision could be UNCERTAIN, but should_split is
    unambiguously False. A caller that checks `decision == SPLIT` gets this for free; one that
    is refactored to check something else must not silently start treating UNCERTAIN as
    permission -- this test exists so such a refactor breaks loudly."""
    text = ("ある案件の詳細を確認してください。" * 30 +
            "(1)背景 (2)経緯 (3)結果")
    v = sp.judge(text)
    assert v.decision == sp.UNCERTAIN
    assert v.should_split is False


def test_long_text_with_no_independence_signal_is_uncertain_not_split():
    """Length alone, once every real independence signal is checked and none fired, is
    evidence of nothing -- this is the direct replacement for the old
    len(text) >= AUTOSTART_FANOUT_MIN_CHARS proxy the plan named as the defect."""
    text = "とても長い一つながりの依頼文です。" * 60
    assert len(text) >= 600
    v = sp.judge(text)
    assert v.decision == sp.UNCERTAIN
    assert v.should_split is False
