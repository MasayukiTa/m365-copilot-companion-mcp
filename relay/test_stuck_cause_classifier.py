# -*- coding: utf-8 -*-
"""A stuck worker's reason starts with its real cause, read from the tool ledger.

Owner requirement 2026-10-15: when the DB host is unreachable the reason must say NETWORK in its
first words. The error strings below are copied from .fleet/tool_events.jsonl (the 2026-10-14
night run), not invented: the 08001/10060 ODBC report, and the lock refusal naming the session IP.
"""
import types

import pytest

from relay import relay_fleet as F
from relay import stuck_cause as S

T0 = 1791971100.0

NET_TEXT = ("returned its own error report [odbc_tables error: OperationalError: ('08001', "
            "'[08001] [Microsoft][ODBC SQL Server Driver][DBNETLIB]SQL Server が存在しないか、"
            "アクセスが拒否されました。 (17) (SQLDriverConnect); [08001] [Microsoft][ODBC SQL Server "
            "Driver][DBNETLIB]ConnectionOpen (Connect()). (10060)')]")
LOCK_TEXT = ("refused (locked) [locked: no valid unlock token for '20.210.146.129'] The identity "
             "in the forwarding header is not sufficient on its own. Call unlock(password="
             "'<password>') and pass the returned `unlock_token` with the call. If you do not have "
             "the password, do not stop: hand the whole instruction to fleet_submit(goal=..., "
             "source=...), which needs no unlock.")
ENV = {"MCP_DB_MAIN": r"DRIVER={SQL Server};SERVER=192.0.2.10\INST,14330;DATABASE=maindb;UID=u;PWD=x;"}


def _pair(i, tool, ts, ok, text="", worker="", conn="main", result=None):
    call = {"schema": 1, "event": "call", "id": "c%d" % i, "ts": ts, "tool": tool, "task": "",
            "worker": worker, "turn": None, "attr": "ambiguous" if not worker else "window",
            "args": {"connection": {"text": conn, "len": len(conn), "truncated": False}}}
    out = {"schema": 1, "event": "outcome", "id": "c%d" % i, "ts": ts + 5, "ok": ok,
           "error": "" if ok else text.split(" [", 1)[0],
           "result": result if result is not None else {"text": text}}
    return [call, out]


def _rows(*specs):
    rows = []
    for i, s in enumerate(specs):
        rows += _pair(i, *s[:1], T0 + 10 * i, *s[1:3], **(s[3] if len(s) > 3 else {}))
    return rows


# (name, specs, kind, code, host, port, ip, attributed)
TABLE = [
    ("odbc 08001/10060 on unattributed rows",
     [("odbc_tables", False, NET_TEXT), ("odbc_query", False, NET_TEXT)],
     "network", "10060", "192.0.2.10", "14330", "", False),
    ("odbc 10060 attributed to the worker",
     [("odbc_query", False, NET_TEXT, {"worker": "w9"})],
     "network", "10060", "192.0.2.10", "14330", "", True),
    ("lock refusal names the session IP",
     [("run_python", False, LOCK_TEXT)],
     "locked", "locked", "", "", "20.210.146.129", False),
    ("a later success closes the network cause",
     [("odbc_query", False, NET_TEXT), ("odbc_query", True, "col\n1")],
     None, "", "", "", "", None),
    ("a later successful unlock closes the lock",
     [("run_python", False, LOCK_TEXT), ("unlock", True, "unlock成功。このIPはアンロック済みです")],
     None, "", "", "", "", None),
    ("content that quotes a timeout is not a failure",
     [("read_file", True, "URL: https://example.test/spot/detail_10060.html\nStatus: 200 timed out")],
     None, "", "", "", "", None),
    ("another worker's failure is not this worker's",
     [("odbc_query", False, NET_TEXT, {"worker": "w3"})],
     None, "", "", "", "", None),
]


@pytest.mark.parametrize("name,specs,kind,code,host,port,ip,attributed", TABLE,
                         ids=[t[0] for t in TABLE])
def test_classifier_table(name, specs, kind, code, host, port, ip, attributed):
    c = S.classify_rows(_rows(*specs), worker="w9", since=0.0, until=T0 + 1000, env=ENV)
    if kind is None:
        assert c is None
        return
    assert (c.kind, c.code, c.host, c.port, c.ip, c.attributed) == (kind, code, host, port, ip,
                                                                   attributed)


def test_network_headline_names_host_code_and_since():
    c = S.classify_rows(_rows(("odbc_query", False, NET_TEXT), ("odbc_query", False, NET_TEXT)),
                        worker="w9", since=0.0, until=T0 + 1000, env=ENV)
    h = c.headline()
    assert h.startswith("ネットワーク: DBサーバ 192.0.2.10:14330 へ到達できません(10060)")
    assert "2回" in h and "同時間帯" in h          # count, and the honest attribution note


def test_lock_headline_is_not_the_network_one():
    c = S.classify_rows(_rows(("run_python", False, LOCK_TEXT)), worker="w1", since=0.0,
                        until=T0 + 1000, env=ENV)
    assert c.headline().startswith("ロック: ") and "20.210.146.129" in c.headline()


def test_generic_wrapper_is_replaced_not_kept_in_front():
    c = S.classify_rows(_rows(("odbc_query", False, NET_TEXT)), worker="w9", since=0.0,
                        until=T0 + 1000, env=ENV)
    old = ("worker reached the same conclusion on consecutive turns -> settling on its own stated "
           "reason instead of nudging again: odbc_queryの再試行は毎回10060タイムアウト")
    new = S.compose_reason(c, old)
    assert new.startswith("ネットワーク:")
    assert "settling on its own stated reason" not in new
    assert "odbc_queryの再試行は毎回10060タイムアウト" in new      # the model's finding survives


# --- the worker-level behaviour (RelayWorker._stamp_stuck_cause) ---------------------------
CANNED_REFUSED = ("⚠ 定型の無回答が継続。ただし本走行の別ワーカーにはカスタムエージェントが応答して"
                  "おり、MCPコネクタは生きている。→ 接続の問題ではなく**この指示に対する拒否**。"
                  "再ナビもヘッドフル復旧も効かない。指示の言い換えが要る。")


def _worker(outcome, reason, goal="過去1か月のデータをDBから集計して", **kw):
    w = types.SimpleNamespace(
        name="w2", status="stuck", outcome=outcome, reason=reason, goal=goal,
        _first_turn_sent_at=T0 - 1, _gateway_unlanded=0, retryable_override=False,
        stuck_cause="", stuck_cause_info={})
    w.__dict__.update(kw)
    return w


def _stamp(w, rows, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    F.RelayWorker._stamp_stuck_cause(w, "running", rows=rows)
    return w


def test_canned_no_answer_is_not_called_a_refusal_when_the_host_is_down(monkeypatch):
    w = _stamp(_worker("REFUSED", CANNED_REFUSED), _rows(("odbc_query", False, NET_TEXT)),
               monkeypatch)
    assert w.reason.startswith("ネットワーク: DBサーバ 192.0.2.10:14330")
    assert "この指示に対する拒否" not in w.reason and "言い換えが要る" not in w.reason
    assert w.outcome == "INFRA_STUCK" and w.retryable_override is None
    assert w.stuck_cause == "network" and w.stuck_cause_info["code"] == "10060"


def test_stuck_on_the_models_own_words_gets_the_typed_cause_first(monkeypatch):
    own = ("worker reached the same conclusion on consecutive turns -> settling on its own stated "
           "reason instead of nudging again: DB照会ツールが恒常的にロック（セッションIP未認可）")
    w = _stamp(_worker("STUCK", own), _rows(("run_python", False, LOCK_TEXT)), monkeypatch)
    assert w.reason.startswith("ロック: ") and "20.210.146.129" in w.reason
    assert "settling on its own stated reason" not in w.reason
    assert w.outcome == "STUCK" and w.stuck_cause == "locked"


def test_an_unrelated_worker_is_not_headlined_with_a_fault_beside_it(monkeypatch):
    w = _stamp(_worker("REFUSED", CANNED_REFUSED, goal="京都の日帰り温泉を3つ調べて"),
               _rows(("odbc_query", False, NET_TEXT)), monkeypatch)
    assert w.reason == CANNED_REFUSED and w.outcome == "REFUSED" and w.stuck_cause == ""


def test_no_cause_in_the_ledger_leaves_the_diagnosis_alone(monkeypatch):
    w = _stamp(_worker("REFUSED", CANNED_REFUSED), [], monkeypatch)
    assert w.reason == CANNED_REFUSED and w.outcome == "REFUSED"


def test_gateway_not_reached_is_typed_and_says_what_the_ledger_saw(monkeypatch):
    old = "⚠ ツール呼び出しが2 ターン連続でゲートウェイに到達していない。"
    w = _stamp(_worker("INFRA_STUCK", old, goal="x", _gateway_unlanded=2), [], monkeypatch)
    assert w.reason.startswith("ゲートウェイ未到達:") and "1件も" in w.reason
    assert w.stuck_cause == "gateway" and w.outcome == "INFRA_STUCK"


def test_stamping_twice_does_not_stack(monkeypatch):
    w = _stamp(_worker("REFUSED", CANNED_REFUSED), _rows(("odbc_query", False, NET_TEXT)),
               monkeypatch)
    first = w.reason
    F.RelayWorker._stamp_stuck_cause(w, "running", rows=_rows(("odbc_query", False, NET_TEXT)))
    assert w.reason == first


def test_an_unattributed_network_cause_is_put_on_a_worker_whose_procedure_declares_the_failing_tool():
    """The goal's wording is not consulted: the domain lives in the procedure (its declared
    `requires_tools`), never in the harness. A goal with no technology word at all gets the cause
    when its matched procedure needs the tool that failed, and does not when it needs another."""
    c = S.Cause(kind="network", code="10060", tool="odbc_query", attributed=False)
    goal = "先週のまとめを出して"            # no technology word at all
    assert not S.plausible(c, goal, "")
    assert S.plausible(c, goal, "", required_tools=["odbc_query"])
    assert not S.plausible(c, goal, "", required_tools=["chart_render"])
    assert not S.plausible(c, goal, "", required_tools=[])
