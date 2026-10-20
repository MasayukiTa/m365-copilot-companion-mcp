# -*- coding: utf-8 -*-
"""続きのターンによる STUCK 回復 (relay/continue_recovery.py)。

事故 (ジョブ eb0a78560880, 2026-10-19): 作用する目標の 1 ターンが、50 回超のツール実行のあと
`turn deadline exceeded before a completion frame` で切れた。艦隊は「届いたかもしれないので再送しない」
と STUCK にしたが、作業そのものは済んでいた。

ここで固定するのは二重実行を起こさないことで、順番は次のとおり。
  1. 設定が off なら、挙動は今までと同じ(記録を持たず、送信文に何も足さず、拒否の文言も同じ)。
  2. 実行は済んだが結果をまだ送り返していない呼び出しを、もう一度実行しない。
  3. 会話が無い(空の会話に続きを送った)なら STUCK のまま。再送も新会話もない。
  4. 呼び出しの最中・結果不明・台帳に戻らない call・旧スレッド生存・目標の種類・回数上限は、続けない。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import chathub as CH                       # noqa: E402
from relay import relay_fleet as rf                   # noqa: E402
from relay.socket_driver import CopilotSocketDriver   # noqa: E402
from tools import settings_path as SP                 # noqa: E402

DEADLINE = "ChatHubError: turn deadline exceeded before a completion frame"
GOAL = "新しい worktree を作成してください。修正して git commit し、テストを保存してください。"
CATALOGUE = [{"name": "call_tool", "description": "gateway"}]


# ---- 補助 ----------------------------------------------------------------------------------

def _token(exp_in=3600):
    import base64
    raw = base64.urlsafe_b64encode(json.dumps(
        {"exp": int(time.time()) + exp_in, "oid": "o", "tid": "t"}).encode()).decode().rstrip("=")
    return "eyJ0eXAiOiJKV1QifQ." + raw + ".sig"


def _update(text):
    return json.dumps({"type": 1, "target": "update",
                       "arguments": [{"writeAtCursor": text}]}) + CH.RS


def _done():
    return json.dumps({"type": 3}) + CH.RS


def _declined(value="InvalidRequest"):
    return json.dumps({"type": 2, "item": {"result": {"value": value}}}) + CH.RS


class _Sock:
    """台本どおりに返すソケット。台本の項目が関数なら、読む瞬間に評価する。"""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def send(self, blob):
        self.sent.append(blob)

    def recv(self, _timeout):
        if not self.script:
            return None
        item = self.script.pop(0)
        return item() if callable(item) else item

    def close(self):
        pass


def _connect(*scripts):
    made = []

    def connect(_url, _headers, _timeout):
        if len(made) >= len(scripts):
            raise CH.ChatHubError("no more scripted sockets")
        script = scripts[len(made)]
        if isinstance(script, Exception):
            made.append(None)
            raise script
        made.append(_Sock(script))
        return made[-1]

    connect.made = made
    return connect


def _texts(sock):
    """この接続で送った依頼文(chat フレームの message.text)。"""
    out = []
    for blob in sock.sent:
        for part in blob.split(CH.RS):
            if '"target": "chat"' in part:
                out.append(json.loads(part)["arguments"][0]["message"]["text"])
    return out


def _all_texts(connect):
    return [t for s in connect.made if s is not None for t in _texts(s)]


def _refs(connect):
    return re.findall(r"\[ref:([0-9a-f]{12})\]", "\n".join(_all_texts(connect)))


def _call(args):
    return "```call_tool\n" + json.dumps(args, ensure_ascii=False) + "\n```"


class _Tools:
    """run_tool の代役。何回、何を実行したかを数える。"""

    def __init__(self):
        self.ran = []

    def __call__(self, name, args):
        self.ran.append((name, json.dumps(args, sort_keys=True)))
        return True, "RESULT-OF-" + str(args.get("name"))

    def count(self, what):
        return sum(1 for _n, a in self.ran if what in a)


def _mode(tmp_path, monkeypatch, value):
    path = tmp_path / "settings.txt"
    if value is None:
        path.write_text("", encoding="utf-8")
    else:
        path.write_text("socket_continue_recovery=%s\n" % value, encoding="utf-8")
    monkeypatch.setattr(SP, "NEW_PATH", str(path))
    from relay import continue_recovery as CR
    CR._cache.clear()
    return path


def _driver(connect, tools):
    conv = CH.Conversation(lambda: _token())
    conv.conversation_id = "conv-xyz"
    conv.turns = 1
    drv = CopilotSocketDriver(conv, connect)
    drv.use_tools(CATALOGUE, tools)
    return drv


def _run(drv, text, timeout=15.0):
    drv.send(text)
    assert drv.wait_for_idle(timeout), "the turn did not finish"


class _Route:
    def __init__(self, drivers):
        self.drivers = list(drivers)
        self.asked = []
        self.records = []

    def open(self):
        return True

    def driver_for(self, name, **kw):
        self.asked.append(dict(kw, name=name))
        return self.drivers.pop(0) if self.drivers else None

    def note_failure(self, why):
        pass

    def note_success(self):
        pass

    def note_stall(self, name, why):
        pass

    def record(self, event, **f):
        self.records.append(dict(f, event=event))

    def token_life(self, agent_url=None):
        return 1800.0


def _worker(monkeypatch, route, drv, goal=GOAL):
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: pytest.fail("a tab must not be opened"))
    from relay import continue_recovery as CR
    monkeypatch.setattr(CR, "read_ledger_tail", lambda *a, **k: [])
    w = rf.RelayWorker(goal, "w0")
    w.socket, w.drv, w.status = True, drv, "waiting"
    w.job = "THE-JOB"
    return w


def _cut_after_one_call(tools, *, second="silent"):
    """1 往復目で呼び出し A を要求 → こちらで実行 → 2 往復目(結果の送信)で切れる。"""
    A = {"name": "edit_and_verify", "arguments": {"path": "a.py"}}
    first = ["{}" + CH.RS, _update("直します。\n" + _call(A)), _done()]
    if second == "silent":
        cut = ["{}" + CH.RS, _update("途中")]        # 完了フレームなしで無音になる
    else:
        cut = second
    connect = _connect(first, cut)
    drv = _driver(connect, tools)
    return A, connect, drv


# ---- 1. 設定が off なら今までと同じ ------------------------------------------------------------

def test_mode_off_changes_nothing_on_the_wire_or_in_the_refusal(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, None)                       # キー無し = off
    tools = _Tools()
    A, connect, drv = _cut_after_one_call(tools)
    _run(drv, "THE-JOB")
    assert drv.failed and "silent" in drv.failed
    assert getattr(drv.conv, "journal", None) is None, "off なのに記録を持った"
    assert not _refs(connect), "off なのに送信文に参照番号が付いた"
    assert getattr(drv, "_recovery", None) is None

    route = _Route([_driver(_connect(), tools)])
    w = _worker(monkeypatch, route, drv)
    w._landed_pending = True
    assert w._retry_socket(DEADLINE) is False
    assert w.outcome == "STUCK"
    assert w.reason == ("not re-sent: the turn may already have been delivered and this goal "
                        "performs an action (delivery=unknown, %s)" % DEADLINE[:120])
    assert not route.asked, "off なのに新しいドライバを作った"
    assert tools.count("a.py") == 1


def test_the_setting_defaults_to_off_and_is_declared():
    from tools import settings_keys as SK
    from relay import continue_recovery as CR
    assert SK.default("socket_continue_recovery") == "off" == CR.DEFAULT
    assert SK.effect("socket_continue_recovery") == SK.EACH_GATE


def test_the_setting_is_read_again_without_a_restart(tmp_path, monkeypatch):
    from relay import continue_recovery as CR
    _mode(tmp_path, monkeypatch, "on")
    assert CR.mode() == "on"
    _mode(tmp_path, monkeypatch, "off")
    assert CR.mode() == "off"
    _mode(tmp_path, monkeypatch, "yes please")
    assert CR.mode() == "off", "on 以外はすべて off"


# ---- 2. 実行済み・結果未送信の呼び出しを、もう一度実行しない -------------------------------------

def _ref_reply(connect_holder, native="なし"):
    """確認ターンへの返答。直前に受け取った参照番号を、読む瞬間に決める。"""
    def reply():
        refs = _refs(connect_holder[0])
        return _update("REF=%s\nNATIVE=%s" % (refs[-1], native))
    return reply


def test_a_call_executed_but_not_yet_sent_back_is_not_executed_again(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "on")
    tools = _Tools()
    A, connect1, drv1 = _cut_after_one_call(tools)
    _run(drv1, "THE-JOB")
    assert "silent" in drv1.failed
    assert tools.count("a.py") == 1, "前提: A はここまでに 1 回だけ実行された"
    journal = drv1.conv.journal
    assert journal is not None

    # 続き: 確認ターン → (モデルは結果を受け取っていないと思って A をもう一度書く) → 本体
    B = {"name": "run_tests", "arguments": {}}
    holder = [connect1]
    connect2 = _connect(
        ["{}" + CH.RS, _ref_reply(holder), _done()],
        ["{}" + CH.RS, _update("続けます。\n" + _call(A) + "\n" + _call(B)), _done()],
        ["{}" + CH.RS, _update("完了しました。"), _done()],
    )
    drv2 = _driver(connect2, tools)
    route = _Route([drv2])
    w = _worker(monkeypatch, route, drv1)
    assert w._retry_socket("ChatHubError: the socket went silent before the turn completed") is True

    assert w.drv is drv2 and w.status == "ready"
    assert "THE-JOB" not in w.job and GOAL not in w.job, "目標の本文を再送してはいけない"
    assert route.asked[0].get("conversation_id") == "conv-xyz"
    _run(drv2, w.job)

    assert drv2.failed == "", drv2.failed
    assert tools.count("a.py") == 1, "A が 2 回実行された: %r" % tools.ran
    assert tools.count("run_tests") == 1
    assert "完了しました" in drv2.read_last_response()
    wire = _all_texts(connect2)
    assert not any(GOAL in t for t in wire), "目標の本文が会話に再送された"
    assert any("RESULT-OF-edit_and_verify" in t for t in wire), \
        "続きの文に、失敗した往復の結果が再掲されていない"
    assert w.reason == "" or "not re-sent" not in w.reason
    assert w.outcome is None


def test_a_state_where_the_results_were_never_sent_is_continued_too(tmp_path, monkeypatch):
    """状態 A: 結果の送信そのものが(接続に失敗して)出ていない。"""
    _mode(tmp_path, monkeypatch, "on")
    tools = _Tools()
    A = {"name": "edit_and_verify", "arguments": {"path": "a.py"}}
    connect1 = _connect(["{}" + CH.RS, _update(_call(A)), _done()],
                        CH.ChatHubError("could not open the socket: reset"))
    drv1 = _driver(connect1, tools)
    _run(drv1, "THE-JOB")
    assert drv1.failed and tools.count("a.py") == 1

    holder = [connect1]
    connect2 = _connect(["{}" + CH.RS, _ref_reply(holder), _done()],
                        ["{}" + CH.RS, _update("済みました。"), _done()])
    drv2 = _driver(connect2, tools)
    w = _worker(monkeypatch, _Route([drv2]), drv1)
    assert w._retry_socket("ChatHubError: no close frame received or sent") is True
    _run(drv2, w.job)
    assert drv2.failed == "" and tools.count("a.py") == 1


# ---- 3. 会話が無いなら STUCK のまま ------------------------------------------------------------

def _gone_then_poll(tmp_path, monkeypatch, make_probe):
    """make_probe(holder) -> the probe turn's scripted socket. holder[0] is the FIRST (cut)
    connection, whose payloads carry the refs a reply can quote."""
    _mode(tmp_path, monkeypatch, "on")
    tools = _Tools()
    A, connect1, drv1 = _cut_after_one_call(tools)
    _run(drv1, "THE-JOB")
    holder = [connect1]
    connect2 = _connect(make_probe(holder), ["{}" + CH.RS, _update("本体が動いた"), _done()])
    drv2 = _driver(connect2, tools)
    route = _Route([drv2, _driver(_connect(), tools)])
    w = _worker(monkeypatch, route, drv1)
    assert w._retry_socket("ChatHubError: the socket went silent") is True
    _run(drv2, w.job)
    return tools, connect2, drv2, route, w


def test_a_conversation_that_no_longer_exists_stays_stuck(tmp_path, monkeypatch):
    """存在しない会話 ID を渡すと空の会話ができる。参照番号を答えられないので、そこで止まる。"""
    tools, connect2, drv2, route, w = _gone_then_poll(
        tmp_path, monkeypatch,
        lambda h: ["{}" + CH.RS, _update("REF=なし" + chr(10) + "NATIVE=なし"), _done()])
    assert drv2.failed.startswith("continue refused"), drv2.failed
    assert tools.count("a.py") == 1 and len(tools.ran) == 1, "確認の失敗のあとにツールが動いた"
    assert len(connect2.made) == 1, "確認のあとに本体の接続を開いた"

    w.drv, w.status = drv2, "waiting"
    assert w.poll() is True
    assert w.outcome == "STUCK" and "continue refused" in w.reason
    assert len(route.asked) == 1, "STUCK のあとで新しいドライバを作った(再送・新会話)"
    assert not any(GOAL in t for t in _all_texts(connect2))


def test_a_backend_that_declines_the_continue_stays_stuck(tmp_path, monkeypatch):
    tools, connect2, drv2, route, w = _gone_then_poll(
        tmp_path, monkeypatch, lambda h: ["{}" + CH.RS, _declined("InvalidRequest"), _done()])
    assert drv2.failed.startswith("continue refused"), drv2.failed
    assert len(tools.ran) == 1
    w.drv, w.status = drv2, "waiting"
    assert w.poll() is True and w.outcome == "STUCK"
    assert len(route.asked) == 1


def test_a_probe_that_admits_a_native_action_stays_stuck(tmp_path, monkeypatch):
    tools, connect2, drv2, route, w = _gone_then_poll(
        tmp_path, monkeypatch, lambda h: ["{}" + CH.RS, _ref_reply(h, "あり"), _done()])
    assert "outside call_tool" in drv2.failed, drv2.failed       # the ref was right; the NATIVE line was not
    assert drv2.failed.startswith("continue refused")
    assert len(tools.ran) == 1


def test_a_probe_with_a_ref_the_model_could_not_have_seen_is_refused(tmp_path, monkeypatch):
    tools, connect2, drv2, route, w = _gone_then_poll(
        tmp_path, monkeypatch,
        lambda h: ["{}" + CH.RS, _update("REF=" + "ab" * 6 + chr(10) + "NATIVE=なし"), _done()])
    assert drv2.failed.startswith("continue refused") and "not one this conversation" in drv2.failed
    assert len(tools.ran) == 1


# ---- 4. 判定(plan)の個々の穴 -----------------------------------------------------------------

def _journal_with(calls=1, finished=True, text="OK", ok=True, rounds=2, sent_last=False,
                  completed_before=True):
    from relay import continue_recovery as CR
    j = CR.TurnJournal()
    j.completed_exchanges = 3
    j.begin_ask()
    for r in range(rounds):
        j.begin_round(r, "payload-%d" % r)
        if r < rounds - 1:
            j.mark_sent()
            j.mark_completed()
    if sent_last:
        j.mark_sent()
    for n in range(calls):
        idx = j.call_started("call_tool", {"name": "x%d" % n})
        if finished:
            j.call_finished(idx, ok, text)
    return j


def _plan(journal, **kw):
    from relay import continue_recovery as CR
    base = dict(goal=GOAL, reason=DEADLINE, used=0, bridged=True, thread_alive=False,
                conv_id="c1", worker="w0", ledger_rows=[])
    base.update(kw)
    return CR.plan(journal, **base)


def test_a_clean_journal_is_continued(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "on")
    p = _plan(_journal_with())
    assert p.ok, p.why
    assert p.recovery is not None and p.recovery.accept_refs


def test_mode_off_never_plans(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "off")
    p = _plan(_journal_with())
    assert not p.ok and p.why.startswith("mode_off")


@pytest.mark.parametrize("kw,journal,code", [
    ({"bridged": False}, None, "not_bridged"),
    ({}, "NONE", "no_journal"),
    ({"reason": "ChatHubError: the backend declined the request: InvalidRequest"}, None,
     "reason_not_continuable"),
    ({"reason": "ChatHubError: the supplied token has expired"}, None, "reason_not_continuable"),
    ({"thread_alive": True}, None, "old_turn_thread_alive"),
    ({"goal": "渡部さんにメールを送信してください。git commit も行う。"}, None, "goal_class"),
    ({"goal": "予定を作成してください"}, None, "goal_class"),
    ({"used": 2}, None, "cap"),
    ({"conv_id": ""}, None, "conversation_unknown"),
    ({"ledger_rows": None}, None, "ledger_unreadable"),
])
def test_each_unsafe_case_is_refused_by_name(tmp_path, monkeypatch, kw, journal, code):
    _mode(tmp_path, monkeypatch, "on")
    from relay import continue_recovery as CR
    monkeypatch.setattr(CR, "read_ledger_tail", lambda *a, **k: None)
    j = None if journal == "NONE" else _journal_with()
    p = _plan(j, **kw)
    assert not p.ok and p.why.startswith(code), p.why


def test_a_call_still_running_is_never_continued(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "on")
    p = _plan(_journal_with(calls=1, finished=False))
    assert not p.ok and p.why.startswith("call_in_flight"), p.why


@pytest.mark.parametrize("text", [
    "TimeoutError: the tool call took longer than 120 s",
    "ConnectError: all connection attempts failed",
    "ReadTimeout: ",
])
def test_a_call_whose_effect_is_unknown_is_never_continued(tmp_path, monkeypatch, text):
    _mode(tmp_path, monkeypatch, "on")
    p = _plan(_journal_with(ok=False, text=text))
    assert not p.ok and p.why.startswith("call_effect_unknown"), p.why


def test_a_tool_that_reports_its_own_failure_is_not_unknown(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "on")
    p = _plan(_journal_with(ok=False, text="[read_file error: no such file]"))
    assert p.ok, p.why


def test_a_turn_with_no_executed_call_is_left_to_the_existing_policy(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "on")
    p = _plan(_journal_with(calls=0, rounds=1))
    assert not p.ok and p.why.startswith("no_executed_calls"), p.why


def test_a_conversation_without_refs_is_refused(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "on")
    j = _journal_with()
    j.last_ref = ""
    p = _plan(j)
    assert not p.ok and p.why.startswith("no_ref"), p.why


def test_a_server_call_that_never_returned_is_refused(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "on")
    j = _journal_with()
    since = j.asks[-1]["started"]
    rows = [{"event": "call", "id": "c1", "ts": since + 1, "worker": "w0", "tool": "shell_exec"}]
    p = _plan(j, ledger_rows=rows)
    assert not p.ok and p.why.startswith("ledger_orphan"), p.why
    rows.append({"event": "outcome", "id": "c1", "ts": since + 2})
    assert _plan(j, ledger_rows=rows).ok
    other = [{"event": "call", "id": "z", "ts": since + 1, "worker": "w9", "tool": "shell_exec"}]
    assert _plan(j, ledger_rows=other).ok, "別の worker の call を数えた"


# ---- 5. 既存の判定との付き合わせ ---------------------------------------------------------------

def test_a_reading_goal_is_resent_exactly_as_before(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "on")
    route = _Route([_driver(_connect(), _Tools())])
    w = _worker(monkeypatch, route, _driver(_connect(), _Tools()), goal="2026年1月の受信メールを一覧してください")
    w.drv._turn_answered = True
    assert w._retry_socket(DEADLINE) is True
    assert getattr(route.drivers, "__len__", None) is not None
    assert getattr(w.drv, "_recovery", None) is None, "読むだけの目標に回復を使った"


def test_an_ineligible_acting_goal_keeps_the_old_refusal(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "on")
    tools = _Tools()
    drv = _driver(_connect(), tools)
    route = _Route([_driver(_connect(), tools)])
    w = _worker(monkeypatch, route, drv, goal="渡部さんにメールを送信してください")
    drv._turn_answered = True
    assert w._retry_socket(DEADLINE) is False
    assert w.outcome == "STUCK" and "may already have been delivered" in w.reason
    assert not route.asked


# ---- 6. 続きの最初の返信だけが再実行を止められる ----------------------------------------------

def test_the_replay_guard_covers_only_the_first_reply(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "on")
    tools = _Tools()
    A, connect1, drv1 = _cut_after_one_call(tools)
    _run(drv1, "THE-JOB")
    holder = [connect1]
    connect2 = _connect(
        ["{}" + CH.RS, _ref_reply(holder), _done()],
        ["{}" + CH.RS, _update(_call(A)), _done()],             # 最初の返信: A の再要求 → 再生
        ["{}" + CH.RS, _update(_call(A)), _done()],             # 次の返信: 正当な再実行 → 実行する
        ["{}" + CH.RS, _update("終わり"), _done()],
    )
    drv2 = _driver(connect2, tools)
    w = _worker(monkeypatch, _Route([drv2]), drv1)
    assert w._retry_socket("ChatHubError: no close frame received or sent") is True
    _run(drv2, w.job)
    assert drv2.failed == ""
    assert tools.count("a.py") == 2, tools.ran      # 元の 1 回 + 2 つ目の返信の 1 回(1 つ目は再生)


# ---- 7. 記録そのもの ---------------------------------------------------------------------------

def test_the_journal_marks_what_was_sent_and_what_ran(tmp_path, monkeypatch):
    _mode(tmp_path, monkeypatch, "on")
    tools = _Tools()
    A, connect1, drv1 = _cut_after_one_call(tools)
    _run(drv1, "THE-JOB")
    j = drv1.conv.journal
    last = j.asks[-1]
    assert [r["sent"] for r in last["rounds"]] == [True, True]
    assert [r["completed"] for r in last["rounds"]] == [True, False]
    assert [c["state"] for c in last["calls"]] == ["done"]
    assert last["rounds"][1]["raw"].startswith("ツールの実行結果は以下")
    assert j.last_ref == last["rounds"][0]["ref"]


def test_the_ledger_tail_reader_returns_none_when_the_file_is_missing(tmp_path):
    from relay import continue_recovery as CR
    assert CR.read_ledger_tail(str(tmp_path / "missing.jsonl")) is None
