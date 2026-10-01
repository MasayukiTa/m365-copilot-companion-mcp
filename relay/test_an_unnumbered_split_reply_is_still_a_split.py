# -*- coding: utf-8 -*-
"""A valid split was thrown away because the list numbers did not survive the trip.

MEASURED live 2026-10-02 (two runs of a three-things-in-parallel goal): the reply that reached
`fanout.subtasks_from` was four (and three) plain lines then `SUBTASKS_READY`. No numbered run,
no `...:` header, so the strict parses found nothing and the goal ran as ONE worker. The same
text with `1.` `2.` prefixes parses to the children. The fixtures below are synthetic
equivalents of those two reply shapes; no real reply text is kept here.

Also covers the Japanese-aware splittability signals: a dense multi-part Japanese goal under
200 characters used to be judged "short, single lookup".
"""
from __future__ import annotations

import pytest

from relay import fanout as fo
from relay import mechanism_telemetry
from relay import splittability as sp

READY = fo.SUBTASKS_READY

# Synthetic equivalents of the two failing replies.
FOUR_PLAIN = "\n".join([
    "ハッシュ表の仕組みを2文で説明する。",
    "二分木の仕組みを2文で説明する。",
    "キューの仕組みを2文で説明する。",
    "上記3つを比較する1行要約を作成する。",
    READY])

THREE_PLAIN = "\n".join([
    "衝突の処理方法を4文で説明する（連鎖法と開番地法に触れる）。",
    "挿入の処理方法を4文で説明する（根からの比較に触れる）。",
    "ラップアラウンドの処理を4文で説明する（剰余演算に触れる）。",
    READY])


def _numbered(text, mark="%d. "):
    out, n = [], 0
    for line in text.splitlines():
        if READY in line:
            out.append(line)
        else:
            n += 1
            out.append((mark % n) + line)
    return "\n".join(out)


@pytest.fixture
def rows(monkeypatch):
    got = []
    monkeypatch.setattr(mechanism_telemetry, "record",
                        lambda mech, **kw: got.append((mech, kw)) or {})
    return got


# ---- the lost split is recovered ------------------------------------------------------------

def test_four_plain_lines_then_the_terminator_are_four_children(rows):
    got = fo.subtasks_from(FOUR_PLAIN)
    assert len(got) == 4
    assert got[0].startswith("ハッシュ表")
    assert rows and rows[0][0] == "fanout_unnumbered_fallback"
    assert rows[0][1]["extra"] == {"steps": 4}


def test_three_plain_lines_then_the_terminator_are_three_children(rows):
    assert len(fo.subtasks_from(THREE_PLAIN)) == 3


def test_the_same_text_numbered_gives_the_same_children_and_no_fallback_row(rows):
    numbered = fo.subtasks_from(_numbered(FOUR_PLAIN))
    assert rows == []
    assert numbered == fo.subtasks_from(FOUR_PLAIN)
    assert len(rows) == 1                 # only the unnumbered call recorded one


@pytest.mark.parametrize("fmt", ["- {}", "* {}", "・{}", "• {}", "（{n}）{}", "({n}) {}",
                                 "{n}) {}", "{z}．{}"])
def test_other_list_marks_are_tolerated(fmt, rows):
    zen = "０１２３４５６７８９"
    out, n = [], 0
    for line in FOUR_PLAIN.splitlines():
        if READY in line:
            out.append(line)
            continue
        n += 1
        out.append(fmt.format(line, n=n, z=zen[n]))
    assert fo.subtasks_from("\n".join(out)) == fo.subtasks_from(FOUR_PLAIN)


def test_a_blank_line_before_the_terminator_is_fine(rows):
    assert len(fo.subtasks_from(FOUR_PLAIN.replace("\n" + READY, "\n\n" + READY))) == 4


# ---- but nothing is invented ----------------------------------------------------------------

def test_a_preamble_line_is_not_a_child(rows):
    body = "以下のサブタスクに分割します。\n" + FOUR_PLAIN
    assert len(fo.subtasks_from(body)) == 4


def test_a_header_and_text_above_it_are_not_children(rows):
    body = "検討メモ。この件は広いです。\n\n分割案:\n" + FOUR_PLAIN
    assert len(fo.subtasks_from(body)) == 4
    assert all("分割案" not in s and "検討" not in s for s in fo.subtasks_from(body))


def test_a_blank_line_above_the_run_ends_it(rows):
    body = "調査の背景を説明する長めの一文です。\n\n" + THREE_PLAIN
    assert len(fo.subtasks_from(body)) == 3


def test_a_single_line_is_not_a_split(rows):
    assert fo.subtasks_from("全部まとめて実行する。\n" + READY) == []
    assert rows == []


def test_a_decline_is_not_a_split(rows):
    assert fo.subtasks_from("分割は不要です。\nこのまま進めます。\n" + READY) == []
    assert rows == []


def test_an_inline_terminator_is_not_a_terminator_line(rows):
    assert fo.subtasks_from("一つ目の作業を行う。二つ目の作業を行う。" + READY) == []


def test_too_many_lines_are_refused(rows):
    body = "\n".join("対象%d の記録を取得して保存する" % i for i in range(20)) + "\n" + READY
    assert fo.subtasks_from(body) == []
    assert rows == []


def test_an_overlong_line_is_prose_not_a_subtask(rows):
    body = ("あ" * 700) + "\n" + "二つ目の作業を実行して結果を保存する\n" + READY
    assert fo.subtasks_from(body) == []


def test_no_terminator_means_no_fallback(rows):
    assert fo.subtasks_from("\n".join(FOUR_PLAIN.splitlines()[:-1])) == []


# ---- a numbered reply is parsed exactly as before --------------------------------------------

def _old_subtasks_from(resp):
    """subtasks_from as it was before the fallback existed (the 'before' side)."""
    steps = [s.strip() for s in (fo.last_numbered_run(resp) or fo.extract_plan(resp or ""))]
    steps = [s for s in steps if fo.SUBTASKS_READY.upper() not in s.upper()]
    steps = fo._dedupe([s for s in steps if len(s) >= fo.MIN_STEP_CHARS])
    if len(steps) < fo.MIN_CHILDREN or len(steps) > fo.MAX_CHILDREN:
        return []
    steps = fo._drop_trailing_system_merge(steps)
    if len(steps) < fo.MIN_CHILDREN or len(steps) > fo.MAX_CHILDREN:
        return []
    return steps


NUMBERED_REPLIES = [
    "この目標は分割できます。\n\n1. 2026年1月分のメールを取得する\n2. 2026年2月分のメールを取得する\n"
    "3. 2026年3月分のメールを取得する\n\n" + READY,
    "共通の前提:\n1. 日本語で書く\n2. 表形式で出す\n\n1. 北の拠点を調べて記録する\n"
    "2. 南の拠点を調べて記録する\n3. 西の拠点を調べて記録する\n" + READY,
    "実行計画:\n1月分を取得する\n2月分を取得する\n\n" + READY,
    "1. 全部やる\n" + READY,
    "- 一つ目の作業をここで行う\n- 二つ目の作業をここで行う\n" + READY,
    "分割は不要です。このまま進めます。" + READY,
    "1. Collect January records\n2. Read subtask 1 results and validate them\n"
    "3. Collect March records\n" + READY,
    "①一つ目の作業をここで行う\n②二つ目の作業をここで行う\n" + READY,
    "",
]


@pytest.mark.parametrize("reply", NUMBERED_REPLIES)
def test_numbered_and_header_replies_are_byte_identical_to_before(reply, rows):
    assert fo.subtasks_from(reply) == _old_subtasks_from(reply)
    assert rows == []                      # none of them took the fallback


# ---- Japanese-aware splittability -----------------------------------------------------------

@pytest.mark.parametrize("goal", [
    "次の3点を別々に調べてください。(1)ハッシュ表 (2)二分木 (3)キュー",
    "1. 売上表を集計する\n2. 在庫表を集計する\n3. 返品表を集計する",
    "（1）顧客台帳の重複を調べる（2）請求台帳の重複を調べる",
    "第一に議事録を要約し、第二に課題を一覧化してください。",
    "・A社の資料を要約する\n・B社の資料を要約する",
    "これらは互いに無関係なので、それぞれ回答してください。",
    "文書を独立して検証してください。",
    "資料を読む。要点を出す。表にする。誤りを直す。",
])
def test_japanese_multi_part_goals_are_uncertain(goal):
    v = sp.judge(goal)
    assert v.decision == sp.UNCERTAIN and v.should_split is False


@pytest.mark.parametrize("goal", [
    "請求書の合計金額を教えてください。",
    "このファイルの先頭10行を表示してください。",
    "来週の会議室の空きを確認する",
])
def test_a_single_short_japanese_task_is_no_split(goal):
    assert sp.judge(goal).decision == sp.NO_SPLIT


def test_dense_japanese_counts_for_more_than_its_character_count():
    text = "顧客台帳と請求台帳の突合結果について、不一致の原因を調査し、報告書として整理してください" * 3
    assert len(text) < 200 <= sp.weighted_length(text)
    assert sp.judge(text).decision == sp.UNCERTAIN


def test_weighted_length_is_plain_length_without_cjk():
    assert sp.weighted_length("a" * 150) == 150


# English goals take the path they always did: (goal, decision recorded from the code before
# this change).
ENGLISH = [
    ("What is 2+2?", sp.NO_SPLIT),
    ("Please do three separate things: (1) explain a hash map; (2) explain a tree; (3) explain a queue.",
     sp.UNCERTAIN),
    ("1. one thing\n2. another thing\n3. a third thing\n- bullet\n- bullet", sp.NO_SPLIT),
    ("Explain each of these independently and separately, in parallel, one by one. " * 4,
     sp.UNCERTAIN),
    ("Summarize this file.", sp.NO_SPLIT),
]


@pytest.mark.parametrize("goal,decision", ENGLISH)
def test_english_decisions_are_unchanged(goal, decision):
    assert sp.judge(goal).decision == decision
