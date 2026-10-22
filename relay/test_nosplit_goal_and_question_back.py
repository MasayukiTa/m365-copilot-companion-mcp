# -*- coding: utf-8 -*-
"""After NO_SPLIT the worker must be told the goal IN FULL, and an agent that keeps asking
"what do you want me to do" must be handed the goal again.

Observed 2026-10-22 in .fleet/transcripts/r6ad93fba_a0_w1 / _w18 / _w31: right after NO_SPLIT
(w1, w18) or after the first generic nudge (w31) Copilot answered
"どのファイルに対してどのような編集を適用したいか、具体的な編集内容を教えてください。" 2-4 times
while the harness kept sending compact-ledger nudges. The ledger points at "the first message of
this conversation" -- for a fan-out worker that message is the "split this, do NOT execute yet"
prompt. The recycle of w1 then handed those question-back replies to the new conversation as
"what the previous conversation said it did".

Offline only: no Copilot, no browser, no fleet launch.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.relay_fleet as RF                      # noqa: E402
from relay import fanout as fo                      # noqa: E402
from relay import first_reply_check as C            # noqa: E402
from relay import mechanism_telemetry as MT         # noqa: E402
from relay import splittability as sp               # noqa: E402

CONSTRAINT = "出力は必ず日本語で、変更はしない。"
GOAL = "C:/work/repo/relay/relay_fleet.py の goal_ledger を監査して結果を報告する。" + CONSTRAINT
QUESTION_BACK = "どのファイルに対してどのような編集を適用したいか、具体的な編集内容を教えてください。"


class _Tx:
    def __init__(self):
        self.metrics = []

    def metric(self, turn, name, value, **kw):
        self.metrics.append((turn, name, value, kw))

    def assistant(self, *a, **k):
        pass


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(MT, "LOG", str(tmp_path / "mechanisms.jsonl"))
    return lambda: [r for r in MT.load(str(tmp_path / "mechanisms.jsonl"))
                    if r["mechanism"] == "first_message_not_absorbed"]


def _fanout_worker(monkeypatch):
    monkeypatch.setattr(RF._splittability, "judge",
                        lambda *a, **k: sp.Verdict(sp.SPLIT, "forced for the test"))
    w = RF.RelayWorker({"text": GOAL, "depth": 0}, "w0", fanout=True,
                       spawn_fn=lambda *a, **k: None)
    assert w.fanout is True and fo.SPLIT_JOB in w._first_message
    w._tx = _Tx()
    w.turn = 1
    w.status = "waiting"
    return w


def _plain_worker():
    w = RF.RelayWorker(GOAL, "w0", fanout=False)
    w._tx = _Tx()
    w.turn = 2
    w._first_absorbed = True        # the goal was acted on once
    w.status = "waiting"
    return w


# -- Fix A -------------------------------------------------------------------------------------

def test_after_no_split_the_message_carries_the_full_goal(monkeypatch):
    w = _fanout_worker(monkeypatch)
    w._decide("1つの作業なので分割しません。\nNO_SPLIT")
    assert w.status == "ready" and w.fanout is False
    assert GOAL in w.job, "the goal is not in the message sent after NO_SPLIT"
    assert RF.CLOSING_INSTRUCTION in w.job
    assert w.job.count(CONSTRAINT) == 1
    # the pointer at the split prompt (and the split prompt itself) are gone
    assert "最初のメッセージ" not in w.job and fo.SPLIT_JOB not in w.job
    assert "実行はまだしないでください" not in w.job


def test_the_first_message_is_now_the_runnable_one(monkeypatch):
    w = _fanout_worker(monkeypatch)
    w._decide("1つの作業なので分割しません。\nNO_SPLIT")
    assert w._first_message == w.job and fo.SPLIT_JOB not in w._first_message
    assert w._first_absorbed is True


def test_no_split_from_an_unabsorbed_state_does_not_resend_the_split_prompt(monkeypatch):
    """_task_anchor used to return the SPLIT prompt itself while the first message was not yet
    judged absorbed -- the opposite of the instruction."""
    w = _fanout_worker(monkeypatch)
    w._first_absorbed = False
    job = w._direct_execution_job("分割しない判断を受け取りました。下記の目標をこの会話で直接実行してください。")
    assert fo.SPLIT_JOB not in job and GOAL in job


def test_a_midrun_decline_keeps_the_real_first_message():
    w = RF.RelayWorker(GOAL, "w0", fanout=False)
    first = w._first_message
    w._direct_execution_job("x")
    assert w._first_message == first


def test_an_empty_goal_falls_back_to_the_anchor():
    w = RF.RelayWorker("", "w0", fanout=False)
    assert RF.CLOSING_INSTRUCTION in w._direct_execution_job("x")


# -- Fix B -------------------------------------------------------------------------------------

def test_one_question_back_is_nudged_as_before(ledger):
    w = _plain_worker()
    w._decide(QUESTION_BACK)
    assert w._first_redeliveries == 0 and "最初のメッセージ" in w.job and ledger() == []


def test_two_question_backs_in_a_row_resend_the_full_goal(ledger):
    w = _plain_worker()
    w._decide(QUESTION_BACK)
    w.status = "waiting"
    w._decide(QUESTION_BACK)
    assert w._first_redeliveries == 1
    assert w.job.startswith(RF.RelayWorker.FIRST_MESSAGE_LEAD_IN) and GOAL in w.job
    rows = ledger()
    assert len(rows) == 1 and rows[0]["extra"]["reason"].startswith("question_back_repeat")
    assert rows[0]["extra"]["redelivery"] == 1


def test_after_no_split_a_question_back_pair_resends_a_runnable_goal(monkeypatch):
    w = _fanout_worker(monkeypatch)
    w._decide("分割しません。\nNO_SPLIT")
    for _ in range(2):
        w.status = "waiting"
        w._decide(QUESTION_BACK)
    assert GOAL in w.job and fo.SPLIT_JOB not in w.job
    assert w._first_redeliveries == 1


def test_a_work_reply_between_resets_the_streak(ledger):
    w = _plain_worker()
    w._decide(QUESTION_BACK)
    w.status = "waiting"
    w._decide("ファイルを確認しました。\n\nCONTINUE")
    w.status = "waiting"
    w._decide(QUESTION_BACK)
    assert w._first_redeliveries == 0 and ledger() == []


def test_the_same_short_non_work_reply_twice_resends_the_goal():
    w = _plain_worker()
    for _ in range(2):
        w.status = "waiting"
        w._decide("了解しました。")
    assert w._first_redeliveries == 1 and GOAL in w.job


def test_the_same_work_reply_twice_does_not_resend_the_goal():
    w = _plain_worker()
    for _ in range(2):
        w.status = "waiting"
        w._decide("起動した。\n\nCONTINUE")
    assert w._first_redeliveries == 0


def test_the_resend_is_bounded_by_the_existing_redelivery_limit(ledger):
    w = _plain_worker()
    for _ in range(12):
        w.status = "waiting"
        w._decide(QUESTION_BACK)
        if w.status in ("stuck", "done"):
            break
    assert w._first_redeliveries <= RF.RelayWorker.FIRST_MESSAGE_MAX_REDELIVERIES
    assert len(ledger()) <= RF.RelayWorker.FIRST_MESSAGE_MAX_REDELIVERIES


def test_the_question_back_reasons_are_reasons_the_checker_can_return():
    assert set(C.QUESTION_BACK_REASONS) == {"asks_for_goal", "empty_message", "greeting", "offers_help"}
    assert C.first_reply_absorbed(QUESTION_BACK, GOAL) == (False, "asks_for_goal")


# -- Fix C -------------------------------------------------------------------------------------

def test_a_recycle_does_not_pass_question_back_replies_as_what_was_done(tmp_path):
    w = RF.RelayWorker(GOAL, "w0", fanout=False)
    path = tmp_path / "w0.jsonl"
    rows = [QUESTION_BACK, "goal_ledger を読み、欠陥を1件確認した。\n\nCONTINUE", QUESTION_BACK,
            "現在、このコパイロットでは担当者へのエスカレーションが構成されていません。"]
    path.write_text("\n".join(json.dumps({"role": "assistant", "text": t}, ensure_ascii=False)
                              for t in rows), encoding="utf-8")
    w.transcript = str(path)
    note = w._compaction_note()
    assert "欠陥を1件確認した" in note
    assert "具体的な編集内容" not in note and "エスカレーション" not in note
