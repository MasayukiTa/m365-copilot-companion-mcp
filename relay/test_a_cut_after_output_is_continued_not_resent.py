# -*- coding: utf-8 -*-
"""出力を受け取ったあとに接続が切れたとき、同じ目標を再送せず、
同じ会話に「続き」のターンを送って回復することを固定する。

背景 (relay/continue_recovery.py と relay/relay_fleet.py:8329 _retry_socket):
  接続切断 (`no close frame received or sent`) は delivery_status で "unknown" になる
  (relay/transport_policy.py:328-336, 484-500)。出力が出たあとに切れたターンは
  partial_text が残るので saw_output が真となり、`_landed_pending`→`landed` が真になる
  (relay/relay_fleet.py:8367-8388)。acting 目標で観測不能な作用なら、設定 on のとき
  `_plan_continue` が Recovery を返し (relay/relay_fleet.py:8418-8432)、STUCK にせず、
  目標本文を再送しない。設定 off なら `mode_off` で Recovery は None、従来どおり STUCK。

本ファイルは本物のフリートもネットワークも使わない。既存の relay/test_continue_turn_recovery.py の
偽ソケット・偽ルート・偽ツールの仕組みをそのまま再利用する。
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# 既存の回復試験のヘルパをそのまま使う。重複を避けるため再定義しない。
from relay import test_continue_turn_recovery as H   # noqa: E402
from relay import relay_fleet as rf                  # noqa: E402

GOAL = H.GOAL
CLOSE_FRAME = "ChatHubError: no close frame received or sent"


def _cut_after_output(tools):
    """1 往復目で呼び出し A を要求→こちらで実行→結果送信 (2 往復目) で、
    出力が出たあとに無音になる。H._cut_after_one_call の second='silent' がまさにこれ。"""
    return H._cut_after_one_call(tools, second="silent")


def test_a_turn_cut_after_output_is_continued_without_resending_the_goal(tmp_path, monkeypatch):
    """設定 on: 出力受信済みで切断→続きのターンで回復。STUCK にならず、目標本文を再送しない。"""
    H._mode(tmp_path, monkeypatch, "on")
    tools = H._Tools()
    A, connect1, drv1 = _cut_after_output(tools)
    H._run(drv1, "THE-JOB")
    assert drv1.failed and "silent" in drv1.failed
    assert tools.count("a.py") == 1, "前提: A はここまでに 1 回だけ実行"
    # 出力が出ていたことの証拠: journal に実行済み呼び出しが残る。
    journal = drv1.conv.journal
    assert journal is not None

    # 続き: 確認ターン→(モデルは結果未受領と思って A をもう一度書く)→本体。
    B = {"name": "run_tests", "arguments": {}}
    holder = [connect1]
    connect2 = H._connect(
        ["{}" + H.CH.RS, H._ref_reply(holder), H._done()],
        ["{}" + H.CH.RS, H._update("続けます。\n" + H._call(A) + "\n" + H._call(B)), H._done()],
        ["{}" + H.CH.RS, H._update("完了しました。"), H._done()],
    )
    drv2 = H._driver(connect2, tools)
    route = H._Route([drv2])
    w = H._worker(monkeypatch, route, drv1)

    assert w._retry_socket(CLOSE_FRAME) is True, "出力受信済みの切断は続きのターンで回復するはず"
    assert w.drv is drv2 and w.status == "ready"
    assert "THE-JOB" not in w.job and GOAL not in w.job, "目標の本文を再送してはいけない"
    assert route.asked[0].get("conversation_id") == "conv-xyz", "同じ会話を続ける"
    H._run(drv2, w.job)

    assert drv2.failed == "", drv2.failed
    assert tools.count("a.py") == 1, "A が 2 回実行された (重複実行): %r" % tools.ran
    assert tools.count("run_tests") == 1
    wire = H._all_texts(connect2)
    assert not any(GOAL in t for t in wire), "目標の本文が会話に再送された"
    assert any("RESULT-OF-edit_and_verify" in t for t in wire), \
        "続きの文に、失敗した往復の結果が再掲されていない"
    # STUCK になっていないこと。
    assert w.outcome is None
    assert w.reason == "" or "not re-sent" not in w.reason


def test_the_same_cut_stays_stuck_when_recovery_is_off(tmp_path, monkeypatch):
    """設定 off: 同じ切断・出力受信済みでも、従来どおり STUCK。再送も新会話もしない。"""
    H._mode(tmp_path, monkeypatch, None)              # キー無し = off
    tools = H._Tools()
    A, connect1, drv1 = _cut_after_output(tools)
    H._run(drv1, "THE-JOB")
    assert drv1.failed and "silent" in drv1.failed
    assert getattr(drv1.conv, "journal", None) is None, "off なのに記録を持った"
    assert tools.count("a.py") == 1

    route = H._Route([H._driver(H._connect(), tools)])
    w = H._worker(monkeypatch, route, drv1)
    w._landed_pending = True
    assert w._retry_socket(CLOSE_FRAME) is False
    assert w.outcome == "STUCK"
    assert "not re-sent" in w.reason, w.reason
    assert not route.asked, "off なのに新しいドライバを作った (再送・新会話)"
    assert tools.count("a.py") == 1, "off でも A を再実行してはいけない"


def test_close_frame_cut_is_delivery_unknown_not_not_delivered():
    """接続切断の reason は delivery_status で 'unknown'。'not_delivered' と誤判定されると
    再送が安全と誤認されるので、ここが回復経路の前提。"""
    from relay.transport_policy import delivery_status, duplicate_risk
    assert delivery_status(CLOSE_FRAME) == "unknown"
    assert duplicate_risk(CLOSE_FRAME) is True
