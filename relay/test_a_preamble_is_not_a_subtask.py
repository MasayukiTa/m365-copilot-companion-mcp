# -*- coding: utf-8 -*-
"""A correct seven-way split was thrown away because the reply held two numbered lists.

MEASURED on the live run r6aa597a8_a0 (2026-09-13 03:18-04:07). Everything the new fan-out
added worked, and the answer was lost at the last step:

    offline judge on the goal                               : UNCERTAIN (should_split=False)
    turn-1 prompt carried SPLIT_JOB and the NO_SPLIT option  : True
    agent replied SUBTASKS_READY with seven numbered subtasks: True
    subtasks_from(reply)                                     : 0 steps

Under the old rule that UNCERTAIN goal would never have been asked at all. It was asked, the
agent said SPLIT and wrote a clean seven-way partition -- and then:

    extract_plan(reply) -> 13 steps   (bounds 2..12)  => subtasks_from returns []

The reply had 「共通の前提」 numbered 1-6, the constraints every subtask must carry, and then
the numbering RESTARTED at 1 for the seven actual subtasks. `extract_plan` collects every
numbered line in a reply, so thirteen came back and the sanity bound refused all of it. The
50-minute run then did the whole job in one conversation.

THE BOUND IS NOT THE DEFECT and is unchanged. Thirteen items, six of which are shared
constraints, is exactly the mis-parse MAX_CHILDREN exists to refuse. The defect is a parse
that produced thirteen from a list of seven.

The fixture below is the real reply's shape, kept short. `test_the_reply_that_was_thrown_away`
runs against the actual transcript when it is still on this machine, because a fixture I wrote
proves what I think happened and the transcript proves what did.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout as fo          # noqa: E402

TWO_LISTS = """分割が有効です。まず全サブタスクに共通する前提を示します。

【共通の前提】
1. 対象は C:/x/repo。両リポジトリとも一切変更しないこと。報告のみ。
2. 前回報告書で既検討済みの上位3件は再提出しない。
3. 提案は必ず原因を除去するもの。対処療法は書かない。
4. 取り込み先はPython中心のMCPサーバ。TSコードは直輸入不可。
5. 各項目に「場所／何をしているか／なぜ有用か／取り込み障害」を記載。
6. 成果物は各サブタスク指定の中間ファイルに保存し、そのパスを報告。

【サブタスク】
1. `.github/workflows/ci.yml` と関連CI設定の解析。
2. `scripts/coverage-partitions.ts` と未読の検証系スクリプトの解析。
3. `packages/hooks` パッケージの中身の解析。
4. `packages/guard` 配下の未読ガードプラグイン群の解析。
5. `benchmarks/` 配下に振る舞い回帰の検出があるかの解析。
6. `packages/` のその他未読プラグイン群の解析。
7. 統合。サブタスク1〜6の中間ファイルを読み、重複排除のうえ統合する。

SUBTASKS_READY"""

ONE_LIST = """分割します。

1. 1月分のメールを取得して一覧にする。
2. 2月分のメールを取得して一覧にする。
3. 3月分のメールを取得して一覧にする。

SUBTASKS_READY"""


# ── the defect ────────────────────────────────────────────────────────────────────────────

def test_a_numbered_preamble_is_not_counted_as_subtasks():
    steps = fo.subtasks_from(TWO_LISTS)
    assert len(steps) == 7, (
        "前置きの番号付きリストがサブタスクに混ざっている（%d件）-- 上限を超えて分割が"
        "丸ごと捨てられる" % len(steps))
    assert "ci.yml" in steps[0]
    assert steps[-1].startswith("統合")


def test_the_preamble_items_are_gone_not_merely_trimmed():
    """Taking the last seven of thirteen would pass the count and carry the wrong content:
    items 1-6 of the subtask list would be dropped and the preamble's tail kept."""
    steps = fo.subtasks_from(TWO_LISTS)
    joined = "\n".join(steps)
    assert "対処療法" not in joined and "両リポジトリとも一切変更" not in joined


def test_the_ordinary_single_list_is_unchanged():
    steps = fo.subtasks_from(ONE_LIST)
    assert len(steps) == 3 and steps[0].startswith("1月")


# ── the boundary conditions the bound exists for, still held ──────────────────────────────

def test_one_item_is_still_not_a_split():
    assert fo.subtasks_from("1. ぜんぶやる\n\nSUBTASKS_READY") == []


def test_forty_items_are_still_refused():
    body = "\n".join("%d. 範囲%d を取得する" % (i, i) for i in range(1, 41))
    assert fo.subtasks_from(body + "\n\nSUBTASKS_READY") == []


def test_a_reply_with_no_list_still_falls_back_to_the_plan_reader():
    """An agent that writes one step per line under a header is a shape extract_plan already
    handles and last_numbered_run does not, so the fallback has to stay."""
    steps = fo.subtasks_from("実行計画:\n1月分を取得する\n2月分を取得する\n\nSUBTASKS_READY")
    assert len(steps) == 2


def test_the_terminator_is_never_queued_as_a_subtask():
    """A SECOND DEFECT, surfaced by writing the test above rather than by looking for it.

    `extract_plan`'s header-fallback breaks at PLAN_READY -- the PLAN marker -- and has never
    known about SUBTASKS_READY, so on that path the marker line came back as a step. A child
    would have been queued whose entire instruction is the word SUBTASKS_READY, and the
    family's declared size would be one too many, so the merge would wait forever for a slice
    that means nothing.
    """
    steps = fo.subtasks_from("実行計画:\n1月分を取得する\n2月分を取得する\n\nSUBTASKS_READY")
    assert not any("SUBTASKS_READY" in s.upper() for s in steps), steps


def test_full_width_numerals_count_as_numbering():
    body = "１. 範囲Aを取得する\n２. 範囲Bを取得する\n\nSUBTASKS_READY"
    assert len(fo.subtasks_from(body)) == 2


def test_a_restart_is_what_splits_the_runs_not_a_blank_line():
    """A list interrupted by prose but continuing to count up is ONE list. Splitting on blank
    lines or on prose would break that, and this is the shape an agent uses when it explains
    a step before the next one."""
    body = ("1. 範囲Aを取得する\n\nここは補足の説明です。\n\n2. 範囲Bを取得する\n"
            "3. 範囲Cを取得する\n\nSUBTASKS_READY")
    assert len(fo.subtasks_from(body)) == 3


# ── against the transcript, not only against my fixture ───────────────────────────────────

TRANSCRIPT = os.path.join(REPO, ".fleet", "transcripts", "r6aa597a8_a0_w0.jsonl")


@pytest.mark.skipif(not os.path.isfile(TRANSCRIPT),
                    reason="the run this was measured on is not on this machine")
def test_the_reply_that_was_thrown_away_now_parses():
    """The fixture above proves what I think happened; this proves what did."""
    rows = [json.loads(l) for l in io.open(TRANSCRIPT, encoding="utf-8") if l.strip()]
    reply = [r for r in rows
             if r.get("role") == "assistant" and r.get("turn") == 1][0]["text"]
    assert fo.fanout_ready(reply), "前提が崩れている: この返信は SUBTASKS_READY で終わっていた"
    steps = fo.subtasks_from(reply)
    assert len(steps) == 7, (
        "実測された返信が %d 件に解釈されている（当時は0件で分割が捨てられた）" % len(steps))
