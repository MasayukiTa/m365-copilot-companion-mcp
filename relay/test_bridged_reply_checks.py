# -*- coding: utf-8 -*-
"""The connector-less route's replacements for the two checks it skips.

  (1) a tool call written in a form the coordinator does not execute  -> corrected, then escalated
  (2) a claim that call_tool / an existing tool is missing            -> checked against the gateway

The replies below are the forms seen on the connector route (markup written as prose, one of
them the text reported on 2026-10-23) and the forms the architecture note for this route says
fail silently here (a `json` info string, the tool's own name, four backticks, a reply cut off
inside the block). The fabricated result is built from public constants (the SHA-256 of "",
"test" and "foo"), the values a worker reported on 2026-10-23 for a tool it never ran. No
transcript text, name or secret is copied into this file.
"""
from __future__ import annotations

import hashlib
import types

import pytest

import relay.relay_fleet as F
from relay import bridged_reply_checks as BC
from relay import socket_tools as ST

# ---------------------------------------------------------------- the replies

#: The reported text of 2026-10-23: a prefix word, then the connector's markup naming a test tool.
TODAYS = ('court <invoke name="260616test-call_tool">\n'
          '<parameter name="arguments">{}</parameter>\n</invoke>')

JSON_FENCE = '```json\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```'
TOOL_NAME_FENCE = '```read_file\n{"path": "a.txt"}\n```'
LABEL_CASE_FENCE = '```Call_Tool\n{"name": "list_directory", "arguments": {}}\n```'
FOUR_TICKS = '````call_tool\n{"name": "", "arguments": {}}\n````'
CUT_OFF = '調べます。\n```call_tool\n{"name": "read_file", "argum'
GOOD_BLOCK = '```call_tool\n{"name": "", "arguments": {}}\n```'


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


#: A GO verdict made of values no tool produced: hashes of "", "test" and "foo", with the call
#: that was supposed to produce them written as prose.
FABRICATED_GO = (
    "検証を実行しました。\n" + TODAYS + "\n\n"
    "結果:\n"
    "- 入力1 のハッシュ: " + _digest("") + "\n"
    "- 入力2 のハッシュ: " + _digest("test") + "\n"
    "- 入力3 のハッシュ: " + _digest("foo") + "\n"
    "判定: GO\n\nDONE")


# ---------------------------------------------------------------- (1) detection

@pytest.mark.parametrize("reply, shape", [
    (TODAYS, BC.SHAPE_MARKUP),
    (JSON_FENCE, BC.SHAPE_FENCED_JSON),
    (TOOL_NAME_FENCE, BC.SHAPE_FENCED_JSON),
    (LABEL_CASE_FENCE, BC.SHAPE_FENCED_JSON),
    (FOUR_TICKS, BC.SHAPE_FENCE_UNREAD),
    (CUT_OFF, BC.SHAPE_FENCE_UNREAD),
])
def test_a_call_in_a_form_the_coordinator_does_not_run_is_recognised(reply, shape):
    assert shape in BC.call_attempt_shapes(reply, 0)
    # and the premise: the coordinator really does not execute it, and says nothing
    read = ST.read_reply(reply)
    assert read["calls"] == [] and read["unreadable"] == []


def test_function_syntax_in_prose_counts_only_when_no_call_was_executed():
    prose = "call_tool(name='read_file', arguments={'path': 'a.txt'}) を実行します。"
    assert BC.SHAPE_FUNCTION in BC.call_attempt_shapes(prose, 0)
    assert BC.call_attempt_shapes(prose, 3) == []          # a summary of calls that were made
    assert BC.call_attempt_shapes(prose, None) == []       # not known: the strong shapes only


@pytest.mark.parametrize("reply", [
    "",
    "作業が終わりました。ファイルを3件確認しました。\nDONE",
    GOOD_BLOCK,                                            # executed by the coordinator
    '```python\nprint("hi")\n```',
    '```json\n{"name": "Taro", "age": 3}\n```',            # data, not a call
    '```\nplain text\n```',
])
def test_ordinary_replies_are_not_call_attempts(reply):
    assert BC.call_attempt_shapes(reply, 0) == []


def test_the_fabricated_result_reply_is_caught_by_its_unexecuted_call():
    assert BC.is_call_attempt(FABRICATED_GO, 0)
    f = BC.diagnose(FABRICATED_GO, 0)
    assert f.kind == BC.KIND_UNLANDED and BC.SHAPE_MARKUP in f.detail


def test_the_correction_shows_the_exact_block_format():
    f = BC.diagnose(JSON_FENCE, 0)
    assert "```call_tool" in f.steer and '"name"' in f.steer
    # what it shows must be something the coordinator really executes
    block = f.steer[f.steer.index("```call_tool"):]
    block = block[:block.index("```", 3) + 3]
    assert ST.read_reply(block.replace("<ツール名>", "read_file"))["calls"]


# ---------------------------------------------------------------- (2) absence claims

class _Gateway:
    """The gateway's answers for a name: a signature for the names it has, the real 'unknown' text else."""

    def __init__(self, have=(), fail=False):
        self.have, self.fail, self.asked = set(have), fail, []

    def __call__(self, tool, args):
        assert tool == "call_tool" and set(args) == {"name"}, "a lookup must not run anything"
        self.asked.append(args["name"])
        if self.fail:
            raise RuntimeError("down")
        n = args["name"]
        if n in self.have:
            return True, "%s(x)\ndoc" % n
        return True, ("[call_tool: no tool or category named '%s'. Closest tools: a. "
                      "Categories: data, files. call_tool(name='') for the index.]" % n)


@pytest.mark.parametrize("reply, names", [
    ("`validity_judge` というツールは索引に存在しません。", ["validity_judge"]),
    ("validity_judge は見当たりません。別の方法で進めます。", ["validity_judge"]),
    ("The tool 'validity_judge' does not exist in the index.", ["validity_judge"]),
    ("There is no tool named `odbc-probe`.", ["odbc-probe"]),
    ("a_tool_x is not available and b_tool_y was not found.", ["a_tool_x", "b_tool_y"]),
])
def test_names_in_a_sentence_that_says_something_is_missing(reply, names):
    assert BC.tools_claimed_missing(reply) == names


@pytest.mark.parametrize("reply", [
    "ファイル a.txt が見つかりません。",                    # no tool-like name
    "validity_judge を実行しました。結果は良好です。",     # no claim of absence
    "call_tool は索引にありません。",                       # the gateway itself has its own check
])
def test_sentences_without_a_claim_about_a_tool_name_yield_no_candidate(reply):
    assert BC.tools_claimed_missing(reply) == []


def test_a_claimed_absence_is_corrected_only_when_the_gateway_has_the_name():
    reply = "`validity_judge` というツールは索引に存在しません。"
    f = BC.diagnose(reply, 0, _Gateway(have={"validity_judge"}))
    assert f.kind == BC.KIND_DENIED_TOOL and f.detail == "validity_judge"
    assert "索引に validity_judge があります" in f.steer
    assert "call_tool(name='validity_judge')" in f.steer
    # the gateway really lacks it: the worker is right, nothing to correct
    assert BC.diagnose(reply, 0, _Gateway(have=set())) is None


def test_a_lookup_that_fails_or_hangs_corrects_nothing():
    reply = "`validity_judge` というツールは索引に存在しません。"
    assert BC.diagnose(reply, 0, _Gateway(fail=True)) is None
    import threading
    never = threading.Event()
    assert BC.exists_in_gateway("x_y", lambda t, a: never.wait(30) or (True, ""), timeout_s=0.1) is None
    never.set()


def test_the_lookup_is_bounded_in_count():
    reply = "a_a_a b_b_b c_c_c d_d_d は存在しません。"
    gw = _Gateway(have=())
    BC.diagnose(reply, 0, gw)
    assert len(gw.asked) <= BC.MAX_CLAIMED_NAMES


@pytest.mark.parametrize("reply", [
    "私の関数一覧に call_tool はありません。",
    "I do not have call_tool in my function list.",
])
def test_saying_there_is_no_call_tool_is_always_corrected_on_this_route(reply):
    f = BC.diagnose(reply, 0)
    assert f.kind == BC.KIND_NO_GATEWAY and "call_tool はあります" in f.steer


def test_a_clean_reply_has_no_fault():
    assert BC.diagnose("できました。DONE", 2, _Gateway()) is None


# ---------------------------------------------------------------- the fleet wiring

GOAL = "テスト用のゴール"


@pytest.fixture()
def worker(monkeypatch):
    monkeypatch.setattr(F, "_unlock_password", lambda: "pw-placeholder-not-a-credential")
    monkeypatch.setattr(F, "connector_proof_source", lambda: "run")
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    w = F.RelayWorker(GOAL, "w0")
    w._last_sent_job = "FIRST MESSAGE"
    w.socket = True
    return w


def _bridged(w, executed=0, gateway=None):
    w.drv = types.SimpleNamespace(_run_tool=gateway or _Gateway(),
                                  conv=types.SimpleNamespace(_tool_calls=executed))
    return w


def _decide(w, resp, turn):
    w.turn = turn
    w.status = "waiting"
    w._decide(resp)


def test_first_unlanded_call_gets_one_correction_with_the_format(worker):
    _bridged(worker)
    _decide(worker, TODAYS, 1)
    assert worker.status == "ready" and worker._unlanded_calls == 1
    assert "```call_tool" in worker.job and "実行されていません" in worker.job


def test_the_second_consecutive_one_opens_a_fresh_conversation(worker):
    _bridged(worker)
    _decide(worker, TODAYS, 1)
    _decide(worker, JSON_FENCE, 2)
    assert worker._malformed_recycles == 1 and worker._unlanded_calls == 0


def test_the_fresh_conversation_retries_are_bounded_then_infra_stuck(worker):
    turn = 0
    for _ in range(2 * (F.FRESH_RETRY_MAX + 1)):
        turn += 1
        _bridged(worker)        # a fresh conversation gets a new bridged driver from the route
        _decide(worker, TODAYS + "\nturn %d" % turn, turn)
        if worker.status == "stuck":
            break
    assert worker.status == "stuck" and worker.outcome == "INFRA_STUCK"
    assert worker._malformed_recycles == F.FRESH_RETRY_MAX
    assert "コネクタなし経路" in worker.reason and "unlanded" in worker.reason


def test_a_clean_turn_ends_the_streak(worker):
    _bridged(worker)
    _decide(worker, TODAYS, 1)
    _decide(worker, "確認しました。続けます。step 2", 2)
    _decide(worker, JSON_FENCE, 3)
    assert worker._unlanded_calls == 1 and worker._malformed_recycles == 0


def test_a_fabricated_result_is_not_accepted_as_done(worker):
    _bridged(worker)
    _decide(worker, FABRICATED_GO, 1)
    assert worker.status == "ready" and worker.outcome != "DONE"
    assert "```call_tool" in worker.job


def test_a_denied_tool_that_the_gateway_has_is_corrected_in_the_fleet(worker):
    _bridged(worker, gateway=_Gateway(have={"validity_judge"}))
    _decide(worker, "`validity_judge` というツールは索引に存在しません。DONE", 1)
    assert worker.status == "ready" and "索引に validity_judge があります" in worker.job


def test_a_denied_tool_that_really_is_absent_is_left_alone(worker):
    _bridged(worker, gateway=_Gateway(have=set()))
    _decide(worker, "`zzz_nothing` というツールは索引に存在しません。別の方法で進めます。", 1)
    assert worker._unlanded_calls == 0 and "索引に" not in (worker.job or "")


def test_a_connector_worker_is_not_judged_by_these_checks(worker):
    worker.drv = types.SimpleNamespace(_run_tool=None)
    assert worker._bridged_reply_fault(TODAYS) is False
    assert worker._bridged_reply_fault("私の関数一覧に call_tool はありません。") is False
